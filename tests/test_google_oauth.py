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
from leadpilot.models.rep_google_credential import RepGoogleCredential


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
    # A test may have connected a (fake) Google credential for this
    # rep — that row's FK to reps.rep_id must go before the rep itself.
    cleanup.query(RepGoogleCredential).filter_by(rep_id=rep_id).delete()
    cleanup.query(Rep).filter_by(rep_id=rep_id).delete()
    cleanup.commit()
    cleanup.close()


def test_build_authorization_url_returns_a_usable_signed_verifier():
    """Direct test of the module function, below the HTTP layer —
    proves build_authorization_url's returned verifier actually
    unsigns back to a real PKCE code_verifier, not just that a cookie
    gets set somewhere.
    """
    from leadpilot import google_oauth

    url, signed_verifier = google_oauth.build_authorization_url(state="test-state")
    assert "code_challenge=" in url

    verifier = google_oauth._unsign_code_verifier(signed_verifier)
    assert verifier is not None
    assert len(verifier) == 128  # google-auth-oauthlib's PKCE verifier length


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


def test_connect_sets_pkce_code_verifier_cookie(logged_in_client):
    """Regression test for the real bug this caught live: google-auth-
    oauthlib generates a PKCE code_verifier per Flow instance and
    google-auth-oauthlib's Flow needs it back at token-exchange time —
    without carrying it across the two separate requests the same way
    state is carried, Google rejects the exchange with
    "(invalid_grant) Missing code verifier."
    """
    response = logged_in_client.get("/auth/google/connect", follow_redirects=False)
    assert "code_challenge=" in response.headers["location"]
    assert logged_in_client.cookies.get("leadpilot_google_oauth_code_verifier") is not None


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


def test_callback_rejects_missing_code_verifier_cookie(logged_in_client):
    """Valid state, but the PKCE code_verifier cookie is gone (e.g.
    expired, or cleared) — must be rejected explicitly, not silently
    passed through to Google only to fail there with a confusing
    "Missing code verifier" error.
    """
    logged_in_client.get("/auth/google/connect", follow_redirects=False)
    real_state = logged_in_client.cookies.get("leadpilot_google_oauth_state")
    del logged_in_client.cookies["leadpilot_google_oauth_code_verifier"]

    response = logged_in_client.get("/auth/google/callback", params={"code": "fake-code", "state": real_state})
    assert response.status_code == 400
    assert "verifier" in response.json()["detail"].lower()


def test_access_token_requires_login():
    client = TestClient(app)
    response = client.get("/auth/google/access-token")
    assert response.status_code == 401


def test_grant_file_requires_login():
    client = TestClient(app)
    response = client.post("/auth/google/grant-file", json={"file_id": "some-file"})
    assert response.status_code == 401


def test_grant_file_for_unconnected_rep_is_404(logged_in_client):
    response = logged_in_client.post("/auth/google/grant-file", json={"file_id": "some-file"})
    assert response.status_code == 404


def test_grant_file_persists_for_connected_rep(logged_in_client):
    # Simulate a completed OAuth connection directly — the actual
    # connect/callback round trip needs a real Google consent screen,
    # tested separately/live. This just proves grant-file's own logic:
    # store, then read back.
    from leadpilot import google_credentials
    from leadpilot.db import SessionLocal

    whoami = logged_in_client.get("/whoami").json()
    session = SessionLocal()
    google_credentials.store_credential(session, uuid.UUID(whoami["rep_id"]), "fake-refresh-token")
    session.commit()
    session.close()

    response = logged_in_client.post("/auth/google/grant-file", json={"file_id": "sheet-abc"})
    assert response.status_code == 200
    assert response.json()["granted_file_ids"] == ["sheet-abc"]

    response2 = logged_in_client.post("/auth/google/grant-file", json={"file_id": "sheet-xyz"})
    assert set(response2.json()["granted_file_ids"]) == {"sheet-abc", "sheet-xyz"}


def test_access_token_for_rep_who_never_connected_google_is_404(logged_in_client):
    response = logged_in_client.get("/auth/google/access-token")
    assert response.status_code == 404
