"""Test doubles shared across tool tests.

FakeLeadSourceConnector is a real, working implementation of
LeadSourceConnector — not a mock with canned assertions — backed by an
in-memory dict instead of a real Google API call. This is exactly what
the LeadSourceConnector abstraction (connectors/base.py) exists for:
swappable lead sources. Using it in tests exercises the actual
interface contract tools are written against, just with a fast,
deterministic data source instead of live Google Sheets — the
connector's own correctness (GoogleSheetsConnector specifically) has
its own real-API tests in test_google_sheets_connector_live.py.
"""

from sqlalchemy.orm import Session

from leadpilot.connectors.base import ChangesSummary, FieldDiff, LeadRecord, LeadSourceConnector


class FakeLeadSourceConnector(LeadSourceConnector):
    def __init__(self, rows_by_source: dict[str, list[LeadRecord]]):
        self._rows_by_source = rows_by_source
        self._writes: list[tuple[str, str, str, str]] = []  # (source_id, row_ref, field, value)

    def list_sources(self) -> list[str]:
        return list(self._rows_by_source.keys())

    def fetch_rows(self, source_id: str) -> list[LeadRecord]:
        # Matches GoogleSheetsConnector's real contract: an unknown/
        # ungranted source_id raises, it doesn't quietly return [] —
        # tools relying on that distinction (e.g. fetch_ad_hoc_sheet's
        # "let the connector's own validation surface naturally") need
        # the fake to actually enforce it, not just simulate the happy
        # path.
        if source_id not in self._rows_by_source:
            raise ValueError(f"Source {source_id!r} not found/granted")
        return list(self._rows_by_source[source_id])

    def stage_field_write(self, source_id: str, row_ref: str, field_name: str, value: str) -> FieldDiff:
        for record in self._rows_by_source.get(source_id, []):
            if record.row_ref == row_ref:
                current = getattr(record, field_name, None)
                return FieldDiff(source_id=source_id, row_ref=row_ref, field=field_name, current=current, proposed=value)
        raise ValueError(f"Row {row_ref!r} not found in source {source_id!r}")

    def commit_field_write(self, source_id: str, row_ref: str, field_name: str, value: str) -> None:
        self._writes.append((source_id, row_ref, field_name, value))
        for record in self._rows_by_source.get(source_id, []):
            if record.row_ref == row_ref:
                setattr(record, field_name, value)

    def detect_changes(self, source_id: str, session: Session) -> ChangesSummary:
        raise NotImplementedError("Not needed by any test using this fake yet")
