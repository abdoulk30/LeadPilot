"""Real end-to-end HTTP tests for what's testable without a live
Google API call: the auth requirement on all three endpoints, and the
CSRF state-cookie verification on /callback. The actual token exchange
(exchange_code_for_refresh_token, get_fresh_access_token's refresh
call) needs a real Google consent flow — see
tests/test_google_sheets_connector_live.py's precedent for that kind
of test, and the manual live walkthrough this was verified against.
"""

import uuid

import pytest
from fastapi.testclient import TestClient

from leadpilot import auth
from leadpilot.app import app
from leadpilot.db import SessionLocal
from leadpilot.models.rep import Rep, RepSession


def _unique_email() -> str:
    return f"{uuid.uuid4()}@example.com"


@pytest.fixture()
def logged_in_client():
    """Same real-commit-then-cleanup pattern as test_app.py's
    committed_rep, but yields an already-logged-in TestClient directly
    since every test here needs one.
    """
    email = _unique_email()
    session = SessionLocal()
    rep = auth.create_rep(session, email=email, password="testpassword123")
    session.commit()
    rep_id = rep.rep_id
    session.close()

    client = TestClient(app)
    client.post("/login", json={"email": email, "password": "testpassword123"})

    yield client

    cleanup = SessionLocal()
    cleanup.query(RepSession).filter_by(rep_id=rep_id).delete()
    cleanup.query(Rep).filter_by(rep_id=rep_id).delete()
    cleanup.commit()
    cleanup.close()


def test_connect_requires_login():
    client = TestClient(app)
    response = client.get("/auth/google/connect", follow_redirects=False)
    assert response.status_code == 401


def test_connect_redirects_to_google_and_sets_state_cookie(logged_in_client):
    response = logged_in_client.get("/auth/google/connect", follow_redirects=False)
    assert response.status_code in (302, 307)
    assert response.headers["location"].startswith("https://accounts.google.com/")
    assert "access_type=offline" in response.headers["location"]
    assert "prompt=consent" in response.headers["location"]
    assert "scope=" in response.headers["location"] and "drive.file" in response.headers["location"]
    assert logged_in_client.cookies.get("leadpilot_google_oauth_state") is not None


def test_callback_requires_login():
    client = TestClient(app)
    response = client.get("/auth/google/callback", params={"code": "fake", "state": "fake"})
    assert response.status_code == 401


def test_callback_without_prior_connect_call_is_rejected(logged_in_client):
    # No /connect call happened, so there's no state cookie at all.
    response = logged_in_client.get(
        "/auth/google/callback", params={"code": "fake-code", "state": "some-state"}
    )
    assert response.status_code == 400
    assert "state" in response.json()["detail"].lower()


def test_callback_rejects_mismatched_state(logged_in_client):
    connect_response = logged_in_client.get("/auth/google/connect", follow_redirects=False)
    real_state_cookie = logged_in_client.cookies.get("leadpilot_google_oauth_state")
    assert real_state_cookie is not None

    # Attacker-style forged callback: right cookie, wrong query state
    # (e.g. an attacker tricking a victim into completing a flow tied
    # to the attacker's own Google account).
    response = logged_in_client.get(
        "/auth/google/callback", params={"code": "fake-code", "state": "attacker-supplied-state"}
    )
    assert response.status_code == 400


def test_access_token_requires_login():
    client = TestClient(app)
    response = client.get("/auth/google/access-token")
    assert response.status_code == 401


def test_access_token_for_rep_who_never_connected_google_is_404(logged_in_client):
    response = logged_in_client.get("/auth/google/access-token")
    assert response.status_code == 404
