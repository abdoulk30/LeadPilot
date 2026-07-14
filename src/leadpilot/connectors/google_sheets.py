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
from leadpilot.models.dedup import LeadSourceRow

_CELL_LOCK_STALE_AFTER = timedelta(seconds=30)

_HEADER_TO_FIELD = {
    "Name": "name",
    "Phone": "phone",
    "Email": "email",
    "Company": "company",
    "Status": "status",
}


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
    def __init__(self, session: Session, rep_id: uuid.UUID, sheets_service=None):
        """`sheets_service` is normally left None (a real Sheets API
        client is built lazily from the rep's own OAuth token). Tests
        pass a fake here — same injectable-client pattern already used
        for Slack/Gmail/Twilio in the Step 2 tools — since this
        connector previously had no way to be exercised without live
        Google credentials and network access.
        """
        self._session = session
        self._rep_id = rep_id
        self._service = sheets_service

    def _client(self):
        if self._service is None:
            access_token = google_oauth.get_fresh_access_token(self._session, self._rep_id)
            if access_token is None:
                raise RepNotConnectedError(f"Rep {self._rep_id} has not connected a Google account")
            creds = GoogleCredentials(token=access_token)
            self._service = build("sheets", "v4", credentials=creds)
        return self._service

    def list_sources(self) -> list[str]:
        """This rep's Picker-granted file IDs — not a static
        admin-configured list (Decision 026). Empty list if the rep
        hasn't connected or hasn't granted any files yet; that's a
        valid state, not an error.
        """
        return google_credentials.granted_file_ids(self._session, self._rep_id)

    def _sheet_id_for(self, source_id: str) -> str:
        """source_id IS the Google file ID as of this rework — this
        just confirms the rep actually granted access to it, rather
        than mapping through an admin config the way Step 1 did.
        """
        if source_id not in self.list_sources():
            raise ValueError(
                f"Rep {self._rep_id} has not granted access to source_id {source_id!r}. "
                f"Granted: {self.list_sources()}"
            )
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
            .get(spreadsheetId=sheet_id, range="A1:F1000")
            .execute()
        )
        rows = result.get("values", [])
        if not rows:
            return [], []
        header = rows[0]
        out = []
        for i, row in enumerate(rows[1:], start=2):  # row 1 is the header
            padded = row + [""] * (len(header) - len(row))
            row_dict = dict(zip(header, padded))
            out.append((str(i), row_dict))
        return header, out

    def _fetch_raw_rows(self, source_id: str) -> list[tuple[str, dict[str, str]]]:
        return self._fetch_header_and_rows(source_id)[1]

    def fetch_rows(self, source_id: str) -> list[LeadRecord]:
        records = []
        for row_ref, raw in self._fetch_raw_rows(source_id):
            records.append(
                LeadRecord(
                    source_id=source_id,
                    row_ref=row_ref,
                    name=raw.get("Name") or None,
                    phone=raw.get("Phone") or None,
                    email=raw.get("Email") or None,
                    company=raw.get("Company") or None,
                    status=raw.get("Status") or None,
                    raw=raw,
                )
            )
        return records

    def stage_field_write(self, source_id: str, row_ref: str, field_name: str, value: str) -> FieldDiff:
        for ref, raw in self._fetch_raw_rows(source_id):
            if ref == row_ref:
                header_name = next((h for h, f in _HEADER_TO_FIELD.items() if f == field_name), field_name)
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
        header_name = next((h for h, f in _HEADER_TO_FIELD.items() if f == field_name), None)
        if header_name is None:
            raise ValueError(f"Unknown field: {field_name!r}")

        cell_key = f"{source_id}:{row_ref}:{field_name}"
        held_by = str(self._rep_id)
        if not locks.try_acquire_sheet_cell_lock(self._session, held_by, cell_key, _CELL_LOCK_STALE_AFTER):
            raise ConcurrentWriteError(source_id, row_ref, field_name)

        try:
            header, rows = self._fetch_header_and_rows(source_id)
            if header_name not in header:
                raise ValueError(f"Column {header_name!r} not found in source {source_id!r}'s header row: {header}")

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
