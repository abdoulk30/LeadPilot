"""GoogleSheetsConnector — reworked for per-rep OAuth (Decision 026),
superseding the Step 1 shared-service-account version (Decision 024).

One instance per rep, constructed with that rep's own session + rep_id
rather than a static admin-configured sources dict. Authenticates via
a fresh access token minted from that rep's stored refresh token
(leadpilot.google_oauth.get_fresh_access_token) — never a service
account, never another rep's credential.

source_id is now the Google file ID itself, not an admin-assigned
label — there's no more static GOOGLE_SHEETS_SOURCES config. What a
rep may access is entirely defined by what they granted through the
Google Picker (leadpilot.google_credentials.granted_file_ids), which
is what list_sources() returns and every other method validates
against.

Column mapping is still fixed to the header row: Name, Phone, Email,
Company, Source, Status. Configurable-per-sheet column mapping is
still not built — same known limitation as Step 1, still not this
rework's job to fix.

row_ref is still the 1-indexed sheet row number as a string — same
fragility-to-manual-reordering caveat as Step 1, unchanged by this
rework.

commit_field_write's lock+expected-value check (Decision 034): see
connectors/base.py's module docstring for the full contract. The
implementation here reuses the header+rows read that column-letter
lookup already needed — no extra Sheets API call for the freshness
check.
"""

import re
import uuid
from datetime import timedelta

from google.oauth2.credentials import Credentials as GoogleCredentials
from googleapiclient.discovery import build
from sqlalchemy import select
from sqlalchemy.orm import Session

from leadpilot import google_credentials, google_oauth, locks
from leadpilot.connectors.base import (
    ChangesSummary,
    ConcurrentWriteError,
    FieldDiff,
    LeadRecord,
    LeadSourceConnector,
    StaleWriteError,
)
from leadpilot.connectors.google_drive import GoogleDriveClient, SPREADSHEET_MIME_TYPE
from leadpilot.models.dedup import LeadSourceRow

_CELL_LOCK_STALE_AFTER = timedelta(seconds=30)

_HEADER_TO_FIELD = {
    "Name": "name",
    "Phone": "phone",
    "Email": "email",
    "Company": "company",
    "Status": "status",
}

# Real intake sheets don't use our canonical headers — Marc's first
# live sheet (2026-07-15) had "FIRST NAME"/"LAST NAME"/"PHONE"/"EMAIL"
# and ingested 600+ leads with every mapped field empty. Matching is
# case-insensitive with common synonyms; split first/last names are
# composed in fetch_rows. Writes (stage/commit_field_write) still
# resolve to the sheet's *actual* header via _resolve_header.
_HEADER_SYNONYMS = {
    "name": "name",
    "full name": "name",
    "lead name": "name",
    "phone": "phone",
    "phone number": "phone",
    "mobile": "phone",
    "cell": "phone",
    "email": "email",
    "email address": "email",
    "company": "company",
    "company name": "company",
    "business": "company",
    "business name": "company",
    "status": "status",
    "lead status": "status",
    "stage": "status",
}

_FIRST_NAME_HEADERS = ("first name", "firstname", "first", "owner first name", "contact first name")
_LAST_NAME_HEADERS = ("last name", "lastname", "last", "surname", "owner last name", "contact last name")

# "PHONE 2", "Email #3", "cell 2" etc. — real sheets carry multiple
# contact points per lead (Marc, 2026-07-15). The first non-empty one
# becomes the lead's primary; every column stays in raw_data, which
# the interface shows in full on the lead's Source data panel.
_NUMBERED_SUFFIX = re.compile(r"\s*#?\s*\d+$")


def _normalize_header(header: str) -> str:
    return " ".join(header.strip().lower().split())


def _field_for_header(norm: str) -> str | None:
    field = _HEADER_SYNONYMS.get(norm)
    if field:
        return field
    # numbered variants: "phone 2" -> "phone", "email #3" -> "email"
    base = _NUMBERED_SUFFIX.sub("", norm)
    if base != norm:
        return _HEADER_SYNONYMS.get(base)
    return None


def _map_row_fields(raw: dict[str, str]) -> dict[str, str | None]:
    """raw sheet row -> canonical field dict, tolerant of header case/
    synonyms and numbered variants, composing split first/last name
    columns. Only the *primary* value per field lands here; the full
    row (every extra phone/email and any custom column) is preserved
    verbatim in LeadRecord.raw for display."""
    fields: dict[str, str | None] = {"name": None, "phone": None, "email": None, "company": None, "status": None}
    first = last = None
    for header, value in raw.items():
        value = (value or "").strip() or None
        norm = _normalize_header(header)
        field = _field_for_header(norm)
        if field:
            if fields[field] is None:
                fields[field] = value
        elif norm in _FIRST_NAME_HEADERS:
            first = value
        elif norm in _LAST_NAME_HEADERS:
            last = value
    if fields["name"] is None and (first or last):
        fields["name"] = " ".join(p for p in (first, last) if p)
    return fields


def _header_score(row: list[str]) -> int:
    """How header-like a row is: count of cells mapping to a canonical
    field or a first/last-name column."""
    score = 0
    for cell in row:
        norm = _normalize_header(str(cell))
        if _field_for_header(norm) or norm in _FIRST_NAME_HEADERS or norm in _LAST_NAME_HEADERS:
            score += 1
    return score


def _detect_header_index(rows: list[list[str]], scan: int = 5) -> int:
    """Real sheets don't reliably put headers on row 1 — Marc's sheets
    (2026-07-15) carry a color-legend row that sits above OR below the
    header row depending on the sheet. Score the first few rows for
    header-likeness and pick the best (earliest on ties, row 0 when
    nothing scores). Rows above the header (a legend) are excluded
    from data; legend rows *below* the header are dropped later by
    fetch_rows' no-contact-fields skip."""
    best_idx, best_score = 0, 0
    for idx, row in enumerate(rows[:scan]):
        score = _header_score(row)
        if score > best_score:
            best_idx, best_score = idx, score
    return best_idx


def _resolve_header(header_row: list[str], field_name: str) -> str | None:
    """Find the sheet's actual header for a canonical field — exact
    canonical name first, then synonyms including numbered variants
    (Phone1, Email #2), so lookups and writes land in the real column
    whatever its casing/numbering. Matches the read path's tolerance
    (_field_for_header) exactly."""
    canonical = next((h for h, f in _HEADER_TO_FIELD.items() if f == field_name), None)
    if canonical in header_row:
        return canonical
    for header in header_row:
        if _field_for_header(_normalize_header(header)) == field_name:
            return header
    return None


def _column_letter(index: int) -> str:
    """0-indexed column position -> A1-notation letter(s) (A, B, ...,
    Z, AA, AB, ...). Handles arbitrary width rather than assuming a
    fixed 6-column sheet.
    """
    letters = ""
    index += 1
    while index > 0:
        index, remainder = divmod(index - 1, 26)
        letters = chr(65 + remainder) + letters
    return letters


class RepNotConnectedError(ValueError):
    """Raised by any method here if the rep has no active Google
    connection — distinct from a plain ValueError (e.g. unknown
    source_id) so callers (Step 2's tools) can tell "you need to
    connect Google first" apart from "that sheet ID is wrong" and
    surface the right message/action to the rep.
    """


class GoogleSheetsConnector(LeadSourceConnector):
    def __init__(
        self,
        session: Session,
        rep_id: uuid.UUID,
        sheets_service=None,
        drive_client: GoogleDriveClient | None = None,
    ):
        """`sheets_service` is normally left None (a real Sheets API
        client is built lazily from the rep's own OAuth token). Tests
        pass a fake here — same injectable-client pattern already used
        for Slack/Gmail/Twilio in the Step 2 tools — since this
        connector previously had no way to be exercised without live
        Google credentials and network access. `drive_client` is the
        same idea for the mimeType-filtering calls in list_sources()/
        _sheet_id_for() (Decision 033) — see tests/fakes.py.
        """
        self._session = session
        self._rep_id = rep_id
        self._service = sheets_service
        self._drive_client = drive_client

    def _client(self):
        if self._service is None:
            access_token = google_oauth.get_fresh_access_token(self._session, self._rep_id)
            if access_token is None:
                raise RepNotConnectedError(f"Rep {self._rep_id} has not connected a Google account")
            creds = GoogleCredentials(token=access_token)
            self._service = build("sheets", "v4", credentials=creds)
        return self._service

    def _drive(self) -> GoogleDriveClient:
        if self._drive_client is None:
            self._drive_client = GoogleDriveClient(self._session, self._rep_id)
        return self._drive_client

    def list_sources(self) -> list[str]:
        """This rep's Picker-granted file IDs, filtered down to actual
        spreadsheets — not a static admin-configured list (Decision
        026). Empty list if the rep hasn't connected or hasn't granted
        any files yet; that's a valid state, not an error.

        Filtering matters as of Decision 033: granted_file_ids is one
        flat list shared with verify_drive_contents' folder grants, so
        without this a rep who's granted a Drive folder would have
        fetch_all_leads try to read that folder ID as a spreadsheet and
        get a real 400 from the Sheets API. One Drive metadata lookup
        per granted ID — fine at Phase 1's per-rep grant counts, not
        worth batching yet. Accepts an injected drive_client for
        testing (see tests/fakes.py) rather than always building a real
        GoogleDriveClient — this method now makes real network calls
        where it previously didn't, so tests need a way to opt out of
        that, same reasoning as the `connector`/`client` DI params on
        the Step 2 tools.
        """
        granted = google_credentials.granted_file_ids(self._session, self._rep_id)
        drive = self._drive()
        return [file_id for file_id in granted if drive.mime_type(file_id) == SPREADSHEET_MIME_TYPE]

    def _sheet_id_for(self, source_id: str) -> str:
        """source_id IS the Google file ID as of this rework — this
        just confirms the rep actually granted access to it, rather
        than mapping through an admin config the way Step 1 did.

        Checks the raw granted_file_ids list, not the mimeType-filtered
        list_sources() — deliberately. Rejecting an id the rep never
        granted at all must stay a fast, local, no-network-call check
        (tests/test_google_sheets_connector_live.py asserts this
        explicitly, predating Decision 033's folder grants). "Granted,
        but it's actually a folder not a spreadsheet" is a different,
        rarer failure mode that only costs a network call for IDs that
        really are in the granted list — see the mimeType check below.
        """
        granted = google_credentials.granted_file_ids(self._session, self._rep_id)
        if source_id not in granted:
            raise ValueError(
                f"Rep {self._rep_id} has not granted access to source_id {source_id!r}. Granted: {granted}"
            )
        if self._drive().mime_type(source_id) != SPREADSHEET_MIME_TYPE:
            raise ValueError(f"source_id {source_id!r} is granted but is not a Google Sheet")
        return source_id

    def _fetch_header_and_rows(self, source_id: str) -> tuple[list[str], list[tuple[str, dict[str, str]]]]:
        """Returns (header_row, [(row_ref, {header: value}), ...]).
        The header is read fresh from the sheet every call rather than
        assumed, so a write's column-letter lookup (commit_field_write)
        can never silently desync from the real column order — that's
        exactly the bug a hardcoded parallel column-letter list caused
        in Step 1 (caught by tests/test_google_sheets_connector_live.py).
        """
        sheet_id = self._sheet_id_for(source_id)
        result = (
            self._client()
            .spreadsheets()
            .values()
            # A1:ZZ — fixed narrow windows keep biting (A1:F hid column G;
            # A1:Z hid the Status column landing in AA on Marc's real
            # 26-column sheet). 52 columns covers any plausible intake
            # sheet; widen again before assuming a bug elsewhere.
            .get(spreadsheetId=sheet_id, range="A1:ZZ2000")
            .execute()
        )
        rows = result.get("values", [])
        if not rows:
            return [], []
        header_idx = _detect_header_index(rows)
        header = rows[header_idx]
        out = []
        # row_refs are real 1-based sheet row numbers — writes depend
        # on them landing on the exact row.
        for i, row in enumerate(rows[header_idx + 1:], start=header_idx + 2):
            padded = row + [""] * (len(header) - len(row))
            row_dict = dict(zip(header, padded))
            out.append((str(i), row_dict))
        return header, out

    def _fetch_raw_rows(self, source_id: str) -> list[tuple[str, dict[str, str]]]:
        return self._fetch_header_and_rows(source_id)[1]

    def fetch_rows(self, source_id: str) -> list[LeadRecord]:
        records = []
        for row_ref, raw in self._fetch_raw_rows(source_id):
            fields = _map_row_fields(raw)
            # A row with no name, phone, or email isn't a lead — it's a
            # legend/annotation/blank row (Marc's sheets carry a
            # color-legend row that would otherwise ingest as a junk
            # "(no name)" lead). Skipped, not errored: annotation rows
            # are normal in rep-owned sheets.
            if not (fields["name"] or fields["phone"] or fields["email"]):
                continue
            records.append(
                LeadRecord(
                    source_id=source_id,
                    row_ref=row_ref,
                    name=fields["name"],
                    phone=fields["phone"],
                    email=fields["email"],
                    company=fields["company"],
                    status=fields["status"],
                    raw=raw,
                )
            )
        return records

    def has_field_column(self, source_id: str, field_name: str = "status") -> bool:
        """Whether the sheet has any column that maps to `field_name`
        (canonical name or synonym, any casing)."""
        header, _ = self._fetch_header_and_rows(source_id)
        return _resolve_header(header, field_name) is not None

    def add_status_column(self, source_id: str) -> str:
        """Appends a 'Status' header to the sheet — rep-initiated and
        popup-confirmed in the interface (2026-07-15, Marc's request:
        real intake sheets often lack one, and without it
        update_lead_sheet has nowhere to record pipeline stage).

        Not gate-staged: this is sheet *structure*, tied to no lead,
        and the explicit typed/clicked confirmation IS the rep's
        approval — same rep-initiated precedent as log_call_outcome.
        Idempotent: returns the existing header if one already maps.
        """
        sheet_id = self._sheet_id_for(source_id)
        result = (
            self._client()
            .spreadsheets()
            .values()
            .get(spreadsheetId=sheet_id, range="A1:ZZ2000")
            .execute()
        )
        rows = result.get("values", [])
        header_idx = _detect_header_index(rows) if rows else 0
        header = rows[header_idx] if rows else []
        existing = _resolve_header(header, "status")
        if existing:
            return existing
        col_letter = _column_letter(len(header))
        # Write into the detected header ROW, not row 1 — sheets with a
        # legend row above the headers would otherwise get the new
        # header dropped into the legend.
        self._client().spreadsheets().values().update(
            spreadsheetId=sheet_id,
            range=f"{col_letter}{header_idx + 1}",
            valueInputOption="RAW",
            body={"values": [["Status"]]},
        ).execute()
        return "Status"

    def status_legend_colors(self, source_id: str) -> dict[str, str]:
        """Reads the sheet's color legend (Issue 011, first slice):
        cells in the top rows whose text is a status word and whose
        background is colored — e.g. a green cell containing FUNDED.
        Returns {status_lowercase: "#rrggbb"}. This is the one place
        gridData (formatting) is fetched; the bounded range keeps the
        response small. Sheets without a legend return {}.
        """
        sheet_id = self._sheet_id_for(source_id)
        resp = (
            self._client()
            .spreadsheets()
            .get(
                spreadsheetId=sheet_id,
                ranges=["A1:AA6"],
                includeGridData=True,
                fields="sheets(data(rowData(values(formattedValue,effectiveFormat(backgroundColor)))))",
            )
            .execute()
        )
        colors: dict[str, str] = {}
        for sheet in resp.get("sheets", []):
            for data in sheet.get("data", []):
                for row in data.get("rowData", []):
                    for cell in row.get("values", []):
                        text = (cell.get("formattedValue") or "").strip()
                        bg = (cell.get("effectiveFormat") or {}).get("backgroundColor") or {}
                        if not text or len(text) > 30:
                            continue
                        r = bg.get("red", 0.0)
                        g = bg.get("green", 0.0)
                        b = bg.get("blue", 0.0)
                        # skip default white / near-white / pure black fills
                        if (r > 0.93 and g > 0.93 and b > 0.93) or (r + g + b < 0.05):
                            continue
                        colors[text.lower()] = f"#{int(r * 255):02x}{int(g * 255):02x}{int(b * 255):02x}"
        return colors

    def stage_field_write(self, source_id: str, row_ref: str, field_name: str, value: str) -> FieldDiff:
        for ref, raw in self._fetch_raw_rows(source_id):
            if ref == row_ref:
                header_name = _resolve_header(list(raw.keys()), field_name) or field_name
                current = raw.get(header_name) or None
                return FieldDiff(
                    source_id=source_id, row_ref=row_ref, field=field_name, current=current, proposed=value
                )
        raise ValueError(f"Row {row_ref!r} not found in source {source_id!r}")

    def commit_field_write(
        self, source_id: str, row_ref: str, field_name: str, value: str, *, expected_current: str | None
    ) -> None:
        """See connectors/base.py's module docstring ("Concurrent-write
        note", Decision 034) for the full contract. Two layers here:

        1. A Postgres lock keyed to this exact cell, so two concurrent
           commit_field_write calls (e.g. two reps approving
           conflicting edits within moments of each other) can't both
           read-then-write past each other. In practice this means the
           second call *blocks* until the first finishes (see
           leadpilot.locks.try_acquire_sheet_cell_lock's docstring) —
           it doesn't reject immediately.
        2. Once unblocked (or immediately, if there was no contention),
           re-read the cell's live value and compare to
           `expected_current`. A mismatch — which is exactly what a
           second, just-unblocked racing writer will normally see —
           raises StaleWriteError rather than silently overwriting.
           This is also the only defense against someone editing the
           sheet directly in Google's UI, bypassing LeadPilot (and its
           lock) entirely.
        """
        if field_name not in _HEADER_TO_FIELD.values():
            raise ValueError(f"Unknown field: {field_name!r}")

        cell_key = f"{source_id}:{row_ref}:{field_name}"
        held_by = str(self._rep_id)
        if not locks.try_acquire_sheet_cell_lock(self._session, held_by, cell_key, _CELL_LOCK_STALE_AFTER):
            raise ConcurrentWriteError(source_id, row_ref, field_name)

        try:
            header, rows = self._fetch_header_and_rows(source_id)
            # Resolve against the sheet's real header row (synonyms,
            # any casing) — same tolerance the read path has.
            header_name = _resolve_header(header, field_name)
            if header_name is None:
                raise ValueError(f"No column for field {field_name!r} in source {source_id!r}'s header row: {header}")

            live_current = None
            found_row = False
            for ref, raw in rows:
                if ref == row_ref:
                    found_row = True
                    live_current = raw.get(header_name) or None
                    break
            if not found_row:
                raise ValueError(f"Row {row_ref!r} not found in source {source_id!r}")

            if live_current != expected_current:
                raise StaleWriteError(source_id, row_ref, field_name, expected_current, live_current)

            col_letter = _column_letter(header.index(header_name))
            sheet_id = self._sheet_id_for(source_id)
            self._client().spreadsheets().values().update(
                spreadsheetId=sheet_id,
                range=f"{col_letter}{row_ref}",
                valueInputOption="RAW",
                body={"values": [[value]]},
            ).execute()
        finally:
            locks.release_sheet_cell_lock(self._session, held_by, cell_key)

    def detect_changes(self, source_id: str, session: Session) -> ChangesSummary:
        current_rows = self.fetch_rows(source_id)
        known = {
            r.row_ref: r
            for r in session.execute(
                select(LeadSourceRow).where(LeadSourceRow.source_id == source_id)
            ).scalars()
        }

        new_rows = []
        updated_rows = []
        for record in current_rows:
            existing = known.get(record.row_ref)
            if existing is None:
                new_rows.append(record)
            elif existing.raw_data != record.raw:
                updated_rows.append(record)

        return ChangesSummary(new_rows=new_rows, updated_rows=updated_rows)
