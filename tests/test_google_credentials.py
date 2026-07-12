"""Real tests against real local Postgres and real Fernet encryption —
no mocking the crypto layer, since the point is confirming a token
never round-trips through plaintext in the database.
"""

import uuid

from sqlalchemy import select

from leadpilot import auth, google_credentials
from leadpilot.models.rep_google_credential import RepGoogleCredential


def _make_rep(session) -> uuid.UUID:
    rep = auth.create_rep(session, email=f"{uuid.uuid4()}-rep@example.com", password="testpassword123")
    return rep.rep_id


def test_store_and_get_refresh_token_roundtrips(db_session):
    rep_id = _make_rep(db_session)
    google_credentials.store_credential(db_session, rep_id, "real-refresh-token-abc123")

    assert google_credentials.get_refresh_token(db_session, rep_id) == "real-refresh-token-abc123"


def test_stored_token_is_never_plaintext_in_the_database(db_session):
    rep_id = _make_rep(db_session)
    google_credentials.store_credential(db_session, rep_id, "real-refresh-token-abc123")

    row = db_session.execute(
        select(RepGoogleCredential).where(RepGoogleCredential.rep_id == rep_id)
    ).scalar_one()
    assert "real-refresh-token-abc123" not in row.refresh_token_encrypted


def test_get_refresh_token_for_unconnected_rep_returns_none(db_session):
    rep_id = _make_rep(db_session)
    assert google_credentials.get_refresh_token(db_session, rep_id) is None


def test_reconnect_overwrites_token_and_clears_revocation(db_session):
    rep_id = _make_rep(db_session)
    google_credentials.store_credential(db_session, rep_id, "first-token")
    google_credentials.revoke(db_session, rep_id)
    assert google_credentials.get_refresh_token(db_session, rep_id) is None

    google_credentials.store_credential(db_session, rep_id, "second-token")
    assert google_credentials.get_refresh_token(db_session, rep_id) == "second-token"


def test_granted_file_ids_empty_until_picker_selection(db_session):
    rep_id = _make_rep(db_session)
    google_credentials.store_credential(db_session, rep_id, "a-token")
    assert google_credentials.granted_file_ids(db_session, rep_id) == []


def test_add_granted_file_accumulates(db_session):
    rep_id = _make_rep(db_session)
    google_credentials.store_credential(db_session, rep_id, "a-token")

    google_credentials.add_granted_file(db_session, rep_id, "sheet-1")
    google_credentials.add_granted_file(db_session, rep_id, "sheet-2")

    assert set(google_credentials.granted_file_ids(db_session, rep_id)) == {"sheet-1", "sheet-2"}


def test_add_granted_file_deduplicates(db_session):
    rep_id = _make_rep(db_session)
    google_credentials.store_credential(db_session, rep_id, "a-token")

    google_credentials.add_granted_file(db_session, rep_id, "sheet-1")
    google_credentials.add_granted_file(db_session, rep_id, "sheet-1")

    assert google_credentials.granted_file_ids(db_session, rep_id) == ["sheet-1"]


def test_revoke_hides_token_and_files_but_keeps_the_row(db_session):
    rep_id = _make_rep(db_session)
    google_credentials.store_credential(db_session, rep_id, "a-token")
    google_credentials.add_granted_file(db_session, rep_id, "sheet-1")

    assert google_credentials.revoke(db_session, rep_id) is True

    assert google_credentials.get_refresh_token(db_session, rep_id) is None
    assert google_credentials.granted_file_ids(db_session, rep_id) == []
    # The row itself still exists — soft revoke, not a delete.
    row = db_session.execute(
        select(RepGoogleCredential).where(RepGoogleCredential.rep_id == rep_id)
    ).scalar_one()
    assert row.revoked_at is not None


def test_revoke_for_rep_with_no_credential_row_returns_false(db_session):
    rep_id = _make_rep(db_session)
    assert google_credentials.revoke(db_session, rep_id) is False


def test_two_reps_have_independent_credentials(db_session):
    rep_a = _make_rep(db_session)
    rep_b = _make_rep(db_session)
    google_credentials.store_credential(db_session, rep_a, "rep-a-token")
    google_credentials.store_credential(db_session, rep_b, "rep-b-token")
    google_credentials.add_granted_file(db_session, rep_a, "rep-a-sheet")

    assert google_credentials.get_refresh_token(db_session, rep_a) == "rep-a-token"
    assert google_credentials.get_refresh_token(db_session, rep_b) == "rep-b-token"
    assert google_credentials.granted_file_ids(db_session, rep_a) == ["rep-a-sheet"]
    assert google_credentials.granted_file_ids(db_session, rep_b) == []
