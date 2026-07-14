"""Real tests against real local Postgres. Same fake-client pattern as
the other Step 2 read tools — this tool's own logic (pass-through
shaping, letting "not granted" surface naturally) is what's under test,
not GoogleDriveClient's correctness.
test_live_against_a_real_connected_rep is the one test that hits the
real Drive API, auto-skipped until a rep has granted a real Drive
folder (not just a sheet) via the Picker.
"""

import uuid

from leadpilot import auth
from leadpilot.connectors.google_drive import DriveFileInfo, GoogleDriveClient
from leadpilot.models.rep_google_credential import RepGoogleCredential
from leadpilot.tools import verify_drive_contents
from leadpilot.tools.registry import load_all_tools

from fakes import FakeDriveContentsClient

from sqlalchemy import select

import pytest


def _make_rep(session) -> uuid.UUID:
    rep = auth.create_rep(session, email=f"{uuid.uuid4()}-verify-drive-test@example.com", password="testpassword123")
    session.flush()
    return rep.rep_id


def test_registers_as_a_tool():
    tools = load_all_tools()
    assert "verify_drive_contents" in tools
    assert tools["verify_drive_contents"].handler is verify_drive_contents.run


def test_returns_shaped_file_info(db_session):
    rep_id = _make_rep(db_session)
    client = FakeDriveContentsClient({
        "folder_1": [
            DriveFileInfo(
                file_id="f1", name="bank_statement.pdf", mime_type="application/pdf",
                size_bytes=48213, created_time="2026-07-01T12:00:00Z",
            ),
            DriveFileInfo(
                file_id="f2", name="id_photo.jpg", mime_type="image/jpeg",
                size_bytes=203991, created_time="2026-07-02T09:30:00Z",
            ),
        ],
    })

    results = verify_drive_contents.run(db_session, rep_id, "folder_1", client=client)

    assert results == [
        {
            "file_id": "f1", "name": "bank_statement.pdf", "mime_type": "application/pdf",
            "size_bytes": 48213, "created_time": "2026-07-01T12:00:00Z",
        },
        {
            "file_id": "f2", "name": "id_photo.jpg", "mime_type": "image/jpeg",
            "size_bytes": 203991, "created_time": "2026-07-02T09:30:00Z",
        },
    ]


def test_empty_folder_returns_empty_list(db_session):
    rep_id = _make_rep(db_session)
    client = FakeDriveContentsClient({"empty_folder": []})
    assert verify_drive_contents.run(db_session, rep_id, "empty_folder", client=client) == []


def test_raises_for_an_ungranted_folder(db_session):
    """Doesn't trigger the Picker itself — just lets the client's own
    validation surface naturally, same pattern as fetch_ad_hoc_sheet.
    """
    rep_id = _make_rep(db_session)
    client = FakeDriveContentsClient({"granted_folder": []})
    with pytest.raises(ValueError):
        verify_drive_contents.run(db_session, rep_id, "not_granted_folder", client=client)


def test_live_against_a_real_connected_rep(db_session):
    row = db_session.execute(
        select(RepGoogleCredential).where(RepGoogleCredential.revoked_at.is_(None))
    ).scalars().first()
    if row is None:
        pytest.skip("No rep has a real, active Google connection yet.")

    # granted_file_ids mixes sheet IDs and folder IDs (Decision 026 —
    # one flat per-rep list). is_folder() checks the real Drive-side
    # mimeType rather than guessing — files.list's '<id> in parents'
    # query wouldn't error on a non-folder ID, it'd just silently
    # return no results, so that couldn't tell them apart.
    real_client = GoogleDriveClient(db_session, row.rep_id)
    folder_id = next((c for c in row.granted_file_ids if real_client.is_folder(c)), None)
    if folder_id is None:
        pytest.skip(
            "No granted ID resolves as a Drive folder yet — grant one via the "
            "'3. Pick a Drive folder' button on /dev/picker-test first."
        )

    results = verify_drive_contents.run(db_session, row.rep_id, folder_id, client=real_client)
    assert isinstance(results, list)
