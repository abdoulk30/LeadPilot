"""Real end-to-end HTTP tests for the login/logout/whoami endpoints —
proving the AUTHENTICATION GUARD actually works over real requests and
real cookies, not just as isolated auth.py function calls.
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
def committed_rep():
    """The FastAPI app uses its own DB session per request (app.get_db),
    separate from the rollback-wrapped db_session fixture — so a rep
    needs a real commit to be visible across those requests. Cleans up
    afterward so real-commit tests don't accumulate rows the way the
    other test files avoid (see test_gate.py/test_locks.py's own
    cleanup blocks).
    """
    email = _unique_email()
    session = SessionLocal()
    rep = auth.create_rep(session, email=email, password="testpassword123", display_name="Test Rep")
    session.commit()
    rep_id = rep.rep_id
    session.close()

    yield email

    cleanup = SessionLocal()
    cleanup.query(RepSession).filter_by(rep_id=rep_id).delete()
    cleanup.query(Rep).filter_by(rep_id=rep_id).delete()
    cleanup.commit()
    cleanup.close()


def test_whoami_without_cookie_is_rejected():
    client = TestClient(app)
    response = client.get("/whoami")
    assert response.status_code == 401
    assert "email" not in response.text  # no data leaked on the reject path


def test_login_then_whoami_works(committed_rep):
    email = committed_rep
    client = TestClient(app)
    login_response = client.post("/login", json={"email": email, "password": "testpassword123"})
    assert login_response.status_code == 200
    assert client.cookies.get("leadpilot_session") is not None

    whoami_response = client.get("/whoami")
    assert whoami_response.status_code == 200
    assert whoami_response.json()["email"] == email.lower()


def test_login_with_wrong_password_gets_no_cookie(committed_rep):
    email = committed_rep
    client = TestClient(app)
    response = client.post("/login", json={"email": email, "password": "wrong password"})
    assert response.status_code == 401
    assert client.cookies.get("leadpilot_session") is None


def test_logout_revokes_session(committed_rep):
    email = committed_rep
    client = TestClient(app)
    client.post("/login", json={"email": email, "password": "testpassword123"})
    assert client.get("/whoami").status_code == 200

    logout_response = client.post("/logout")
    assert logout_response.status_code == 200

    # Same cookie the client still has (if it kept it) must no longer work.
    assert client.get("/whoami").status_code == 401
