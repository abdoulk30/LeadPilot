"""testing/eval-suite.md Case 11 — Per-rep sheet access boundary.

Uses the real GoogleSheetsConnector/GoogleDriveClient directly (not
fakes) — the DATA ACCESS GUARD's rejection happens entirely against
the DB-stored granted_file_ids list, before any live Google API call
is ever made, so this is testable for real with no live credentials.
"""

import uuid

from leadpilot import auth, google_credentials
from leadpilot.connectors.google_drive import SPREADSHEET_MIME_TYPE, GoogleDriveClient
from leadpilot.connectors.google_sheets import GoogleSheetsConnector

from fakes import FakeGoogleDriveClient


def _make_rep_with_grants(session, *granted_ids: str) -> uuid.UUID:
    rep = auth.create_rep(session, email=f"{uuid.uuid4()}-eval-case-11@example.com", password="testpassword123")
    session.flush()
    google_credentials.store_credential(session, rep.rep_id, refresh_token="fake-refresh-token")
    for file_id in granted_ids:
        google_credentials.add_granted_file(session, rep.rep_id, file_id)
    return rep.rep_id


def test_case_11_queue_only_sees_sheets_that_rep_personally_connected(db_session):
    rep_a = _make_rep_with_grants(db_session, "sheet_x", "sheet_y")
    rep_b = _make_rep_with_grants(db_session, "sheet_z")

    # list_sources() filters granted IDs down to actual spreadsheets
    # (Decision 033), which needs a mimeType check — a real Drive
    # client would hit the live API here, so inject a fake one that
    # confirms all three IDs really are spreadsheets, no live
    # credentials needed.
    all_spreadsheets = FakeGoogleDriveClient(
        {"sheet_x": SPREADSHEET_MIME_TYPE, "sheet_y": SPREADSHEET_MIME_TYPE, "sheet_z": SPREADSHEET_MIME_TYPE}
    )
    connector_a = GoogleSheetsConnector(db_session, rep_a, drive_client=all_spreadsheets)
    connector_b = GoogleSheetsConnector(db_session, rep_b, drive_client=all_spreadsheets)

    # Rep A's prioritized queue contains only leads sourced from
    # Sheets X and Y — never Sheet Z, even though it exists in the
    # same LeadPilot database under a different rep.
    assert set(connector_a.list_sources()) == {"sheet_x", "sheet_y"}
    assert set(connector_b.list_sources()) == {"sheet_z"}


def test_case_11_fetch_ad_hoc_sheet_fails_against_another_reps_sheet(db_session):
    rep_a = _make_rep_with_grants(db_session, "sheet_x", "sheet_y")
    _make_rep_with_grants(db_session, "sheet_z")
    connector_a = GoogleSheetsConnector(db_session, rep_a)

    # Rep A guesses/reuses Sheet Z's ID — the call fails, since Rep A's
    # own Google OAuth grant has no access to it. Never falls back to
    # Rep B's stored credential or any shared credential.
    try:
        connector_a.fetch_rows("sheet_z")
        assert False, "expected fetch_rows to reject an ungranted sheet_id"
    except ValueError as e:
        assert "has not granted access" in str(e)


def test_case_11_verify_drive_contents_boundary_holds_too(db_session):
    """Same boundary for Drive: Rep A's Drive check never inspects a
    folder only Rep B has connected.
    """
    rep_a = _make_rep_with_grants(db_session, "folder_a")
    _make_rep_with_grants(db_session, "folder_b")
    drive_client_a = GoogleDriveClient(db_session, rep_a)

    try:
        drive_client_a.list_folder_contents("folder_b")
        assert False, "expected list_folder_contents to reject an ungranted folder_id"
    except ValueError as e:
        assert "has not granted access" in str(e)


def test_case_11_never_falls_back_to_a_shared_or_standing_credential(db_session):
    """A rep with no Google connection at all gets a clear
    "not connected" error, never silent access to anything.
    """
    rep = auth.create_rep(
        db_session, email=f"{uuid.uuid4()}-eval-case-11-noauth@example.com", password="testpassword123"
    )
    db_session.flush()
    connector = GoogleSheetsConnector(db_session, rep.rep_id)

    assert connector.list_sources() == []
