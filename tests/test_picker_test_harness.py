"""Real HTTP tests for the /dev/picker-test route — proving it's
reachable in dev and genuinely gated out otherwise, not just trusting
the environment check by reading the code.
"""

from fastapi.testclient import TestClient

from leadpilot import config
from leadpilot.app import app


def test_picker_test_harness_reachable_in_development():
    client = TestClient(app)
    response = client.get("/dev/picker-test")
    assert response.status_code == 200
    assert "Google Picker test harness" in response.text
    assert "google.picker.PickerBuilder" in response.text


def test_picker_test_harness_sets_app_id():
    """Regression test for a real bug caught live: without setAppId,
    Picker still shows files and fires a real "picked" callback, but
    Google never registers the drive.file grant server-side — the
    selection looks successful but the access token still can't read
    the file afterward.
    """
    client = TestClient(app)
    response = client.get("/dev/picker-test")
    assert ".setAppId(" in response.text
    # The project number (segment before the first "-" in the client
    # ID) must actually be present, not just the method call itself.
    from leadpilot.config import settings

    expected_app_id = settings.google_oauth_client_id.split("-")[0]
    assert f".setAppId('{expected_app_id}')" in response.text


def test_picker_test_harness_gated_outside_development(monkeypatch):
    monkeypatch.setattr(config.settings, "environment", "production")
    client = TestClient(app)
    response = client.get("/dev/picker-test")
    assert response.status_code == 404
