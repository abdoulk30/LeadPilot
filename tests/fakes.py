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

from leadpilot.connectors.base import (
    ChangesSummary,
    FieldDiff,
    LeadRecord,
    LeadSourceConnector,
    StaleWriteError,
)
from leadpilot.connectors.google_drive import DriveContentsClient, DriveFileInfo


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

    def commit_field_write(
        self, source_id: str, row_ref: str, field_name: str, value: str, *, expected_current: str | None
    ) -> None:
        # Matches GoogleSheetsConnector's real contract (Decision 034):
        # verify the live value still matches what the rep reviewed
        # before writing, raising StaleWriteError rather than silently
        # overwriting. No ConcurrentWriteError simulation here — that's
        # a connector-level locking concern with its own real coverage
        # in test_google_sheets_connector.py, not something this
        # lightweight, single-threaded fake needs to reproduce.
        for record in self._rows_by_source.get(source_id, []):
            if record.row_ref == row_ref:
                live_current = getattr(record, field_name, None)
                if live_current != expected_current:
                    raise StaleWriteError(source_id, row_ref, field_name, expected_current, live_current)
                self._writes.append((source_id, row_ref, field_name, value))
                setattr(record, field_name, value)
                return
        raise ValueError(f"Row {row_ref!r} not found in source {source_id!r}")

    def detect_changes(self, source_id: str, session: Session) -> ChangesSummary:
        raise NotImplementedError("Not needed by any test using this fake yet")


class FakeDriveContentsClient(DriveContentsClient):
    def __init__(self, files_by_folder: dict[str, list[DriveFileInfo]]):
        self._files_by_folder = files_by_folder

    def list_folder_contents(self, folder_id: str) -> list[DriveFileInfo]:
        # Matches GoogleDriveClient's real contract: an unknown/
        # ungranted folder_id raises, it doesn't quietly return [].
        if folder_id not in self._files_by_folder:
            raise ValueError(f"Folder {folder_id!r} not found/granted")
        return list(self._files_by_folder[folder_id])


class FakeGoogleDriveClient:
    """Not a DriveContentsClient — GoogleSheetsConnector only ever
    calls .mime_type() on its injected drive client (Decision 033's
    list_sources()/_sheet_id_for() filtering), never
    list_folder_contents(), so this only fakes that one method rather
    than implementing an interface it doesn't need.
    """

    def __init__(self, mime_types: dict[str, str]):
        self._mime_types = mime_types

    def mime_type(self, file_id: str) -> str:
        if file_id not in self._mime_types:
            raise ValueError(f"File {file_id!r} not found/granted")
        return self._mime_types[file_id]
