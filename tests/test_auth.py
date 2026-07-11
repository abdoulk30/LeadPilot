"""Real tests against the real local Postgres for leadpilot.auth."""

import uuid
from datetime import timedelta

import pytest

from leadpilot import auth
from leadpilot.models.rep import Rep, RepSession


def _unique_email() -> str:
    return f"{uuid.uuid4()}@example.com"


def test_password_hash_and_verify_roundtrip():
    hashed = auth.hash_password("correct horse battery staple")
    assert auth.verify_password("correct horse battery staple", hashed) is True
    assert auth.verify_password("wrong password", hashed) is False


def test_password_too_long_is_rejected():
    with pytest.raises(ValueError):
        auth.hash_password("x" * 100)


def test_create_rep_and_authenticate(db_session):
    email = _unique_email()
    auth.create_rep(db_session, email=email, password="testpassword123", display_name="Test Rep")

    rep = auth.authenticate(db_session, email=email, password="testpassword123")
    assert rep is not None
    assert rep.email == email.lower()

    assert auth.authenticate(db_session, email=email, password="wrong password") is None
    assert auth.authenticate(db_session, email="nobody@example.com", password="whatever") is None


def test_authenticate_rejects_deactivated_rep(db_session):
    email = _unique_email()
    rep = auth.create_rep(db_session, email=email, password="testpassword123")
    rep.is_active = False
    db_session.flush()

    assert auth.authenticate(db_session, email=email, password="testpassword123") is None


def test_email_is_case_and_whitespace_normalized(db_session):
    auth.create_rep(db_session, email="  Rep@Example.com  ", password="testpassword123")
    assert auth.authenticate(db_session, email="rep@example.com", password="testpassword123") is not None


def test_create_session_and_validate(db_session):
    rep = auth.create_rep(db_session, email=_unique_email(), password="testpassword123")
    token = auth.create_session(db_session, rep.rep_id)

    resolved = auth.get_rep_for_signed_token(db_session, token)
    assert resolved is not None
    assert resolved.rep_id == rep.rep_id


def test_tampered_token_is_rejected(db_session):
    rep = auth.create_rep(db_session, email=_unique_email(), password="testpassword123")
    token = auth.create_session(db_session, rep.rep_id)

    assert auth.get_rep_for_signed_token(db_session, token + "tampered") is None


def test_unknown_session_id_is_rejected(db_session):
    rep = auth.create_rep(db_session, email=_unique_email(), password="testpassword123")
    real_token = auth.create_session(db_session, rep.rep_id)
    db_session.flush()
    # Forge a validly-signed token for a session_id that was never
    # actually created — proves validation checks the DB, not just
    # the signature.
    forged_token = auth._serializer().dumps("session-id-that-does-not-exist")
    assert auth.get_rep_for_signed_token(db_session, forged_token) is None
    # Sanity: the real one still works.
    assert auth.get_rep_for_signed_token(db_session, real_token) is not None


def test_expired_session_is_rejected(db_session):
    rep = auth.create_rep(db_session, email=_unique_email(), password="testpassword123")
    # ttl in the past — already expired the moment it's created.
    token = auth.create_session(db_session, rep.rep_id, ttl=timedelta(seconds=-1))

    assert auth.get_rep_for_signed_token(db_session, token) is None


def test_revoked_session_is_rejected(db_session):
    rep = auth.create_rep(db_session, email=_unique_email(), password="testpassword123")
    token = auth.create_session(db_session, rep.rep_id)
    assert auth.get_rep_for_signed_token(db_session, token) is not None

    assert auth.revoke_session(db_session, token) is True
    assert auth.get_rep_for_signed_token(db_session, token) is None
    # Revoking an already-revoked session is a no-op, not an error.
    assert auth.revoke_session(db_session, token) is False


def test_session_for_deactivated_rep_is_rejected(db_session):
    rep = auth.create_rep(db_session, email=_unique_email(), password="testpassword123")
    token = auth.create_session(db_session, rep.rep_id)
    assert auth.get_rep_for_signed_token(db_session, token) is not None

    rep.is_active = False
    db_session.flush()
    assert auth.get_rep_for_signed_token(db_session, token) is None
