"""GoogleSheetsConnector — the only LeadSourceConnector implemented so
far (Decision 015 / PRD v1.04 3e).

Authenticates as a service account, not via the GOOGLE_OAUTH_CLIENT_ID/
SECRET OAuth-consent flow that commands/README.md originally assumed
for all Google access. Rationale: fetch_all_leads and update_lead_sheet
run inside an unattended, hourly Cron Job — there's no human present to
click through an OAuth consent screen, and no per-rep identity is
relevant to reading/writing a shared business spreadsheet. A service
account (leadpilot.config.settings.google_service_account_key_path)
authenticates directly with no consent flow or refresh-token storage
needed. Confirm with Marc that this makes sense to keep long-term
alongside whatever Gmail-as-the-rep (Step 2, send_lead_email) ends up
needing — that one plausibly does need real per-rep OAuth, since it
has to send *as* a specific rep's own Gmail account.

Column mapping is fixed to the header row: Name, Phone, Email,
Company, Source, Status (see the test sheet set up for Step 1). A real
product would need this configurable per source sheet, since different
marketing partners use different columns (per the PRD's "Siloed intake
channels" problem statement) — that's Step 2 scope, not Step 1.

row_ref is the 1-indexed sheet row number as a string (e.g. "2" for
the first data row, after the header). This is fragile if rows get
manually reordered or deleted in the sheet between runs — flagging
this as a known limitation, not solving it now. A more robust design
(e.g. a hidden per-row LeadPilot ID column) is a reasonable future
improvement, not a Step 1 requirement.
"""

from google.oauth2 import service_account
from googleapiclient.discovery import build
from sqlalchemy import select
from sqlalchemy.orm import Session

from leadpilot.config import settings
from leadpilot.connectors.base import ChangesSummary, FieldDiff, LeadRecord, LeadSourceConnector
from leadpilot.models.dedup import LeadSourceRow

_SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
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


class GoogleSheetsConnector(LeadSourceConnector):
    def __init__(self, key_path: str | None = None, sources: dict[str, str] | None = None):
        self._key_path = key_path or settings.google_service_account_key_path
        self._sources = sources if sources is not None else settings.google_sheets_sources_map()
        self._service = None

    def _client(self):
        if self._service is None:
            creds = service_account.Credentials.from_service_account_file(
                self._key_path, scopes=_SCOPES
            )
            self._service = build("sheets", "v4", credentials=creds)
        return self._service

    def list_sources(self) -> list[str]:
        return list(self._sources.keys())

    def _sheet_id_for(self, source_id: str) -> str:
        if source_id not in self._sources:
            raise ValueError(f"Unknown source_id: {source_id!r}. Configured sources: {list(self._sources)}")
        return self._sources[source_id]

    def _fetch_header_and_rows(self, source_id: str) -> tuple[list[str], list[tuple[str, dict[str, str]]]]:
        """Returns (header_row, [(row_ref, {header: value}), ...]).
        The header is read fresh from the sheet every call rather than
        assumed, so a write's column-letter lookup (commit_field_write)
        can never silently desync from the real column order — that's
        exactly the bug a hardcoded parallel column-letter list caused
        (caught by tests/test_google_sheets_connector_live.py).
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

    def commit_field_write(self, source_id: str, row_ref: str, field_name: str, value: str) -> None:
        header_name = next((h for h, f in _HEADER_TO_FIELD.items() if f == field_name), None)
        if header_name is None:
            raise ValueError(f"Unknown field: {field_name!r}")
        header, _ = self._fetch_header_and_rows(source_id)
        if header_name not in header:
            raise ValueError(f"Column {header_name!r} not found in source {source_id!r}'s header row: {header}")
        col_letter = _column_letter(header.index(header_name))
        sheet_id = self._sheet_id_for(source_id)
        self._client().spreadsheets().values().update(
            spreadsheetId=sheet_id,
            range=f"{col_letter}{row_ref}",
            valueInputOption="RAW",
            body={"values": [[value]]},
        ).execute()

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
