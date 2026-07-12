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


def test_picker_test_harness_gated_outside_development(monkeypatch):
    monkeypatch.setattr(config.settings, "environment", "production")
    client = TestClient(app)
    response = client.get("/dev/picker-test")
    assert response.status_code == 404
