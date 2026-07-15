"""testing/eval-suite.md Case 6 — Unauthorized access attempt.

require_rep (JSON API) and require_rep_ui (Step 3 workspace) both
reject a request with no valid session before any route body runs —
FastAPI's Depends() executes the guard before the handler, so no lead/
contact data can ever be touched. Both guards now also log the
rejected attempt (app.py/ui.py, fixed 2026-07-14 — the PRD line this
case is built from was previously only quoted in a docstring, never
actually implemented).
"""

import logging

from fastapi.testclient import TestClient

from leadpilot.app import app


def test_case_6_json_api_rejects_before_any_tool_call():
    client = TestClient(app)
    response = client.get("/whoami")
    assert response.status_code == 401
    assert "email" not in response.text  # no rep/lead data leaked on the reject path


def test_case_6_ui_rejects_before_any_tool_call():
    client = TestClient(app)
    response = client.get("/", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_case_6_ui_htmx_partial_rejects_with_no_data():
    client = TestClient(app)
    response = client.get("/ui/queue", headers={"HX-Request": "true"})
    assert response.status_code == 401
    assert response.headers["HX-Redirect"] == "/login"
    assert "lead" not in response.text.lower()


def test_case_6_the_attempt_is_logged(caplog):
    client = TestClient(app)
    with caplog.at_level(logging.WARNING, logger="leadpilot.auth_guard"):
        client.get("/whoami")
    assert any("Rejected unauthenticated request" in record.message for record in caplog.records)


def test_case_6_ui_attempt_is_also_logged(caplog):
    client = TestClient(app)
    with caplog.at_level(logging.WARNING, logger="leadpilot.auth_guard"):
        client.get("/ui/queue", headers={"HX-Request": "true"})
    assert any("Rejected unauthenticated UI request" in record.message for record in caplog.records)


def test_case_6_invalid_session_cookie_also_rejected_and_logged(caplog):
    """Not just a missing cookie — a present-but-invalid/expired one
    must be rejected and logged the same way.
    """
    client = TestClient(app)
    client.cookies.set("leadpilot_session", "not-a-real-signed-token")
    with caplog.at_level(logging.WARNING, logger="leadpilot.auth_guard"):
        response = client.get("/whoami")
    assert response.status_code == 401
    assert any("invalid/expired" in record.message for record in caplog.records)
