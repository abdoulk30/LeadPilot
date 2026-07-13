"""Real tests against the real local Postgres, plus a fake Twilio
client for the execute half — no live Twilio API call needed to
verify the staging/gating/send logic. See send_lead_text.py's module
docstring for the current live-verification status (Issue 005).
"""

import uuid
from types import SimpleNamespace

import pytest

from leadpilot import auth, gate
from leadpilot.config import settings
from leadpilot.models.contact_history import Channel, ContactHistory, Stage, Tool
from leadpilot.models.leads import Lead
from leadpilot.tools.base import all_tools
from leadpilot.tools.send_lead_text import execute_send_lead_text, send_lead_text


class FakeTwilioClient:
    """Mimics twilio.rest.Client's .messages.create(...) call without
    a real API request.
    """

    def __init__(self, status: str = "queued"):
        self.calls: list[dict] = []
        self._status = status
        self.messages = self  # so `client.messages.create(...)` resolves to this object's create()

    def create(self, *, body, from_, to):
        self.calls.append({"body": body, "from_": from_, "to": to})
        return SimpleNamespace(sid=f"SM{len(self.calls):032x}", status=self._status)


def _make_lead(session, *, phone: str | None = "+15551234567", name: str = "Test Lead") -> uuid.UUID:
    lead = Lead(display_name=name, primary_phone=phone)
    session.add(lead)
    session.flush()
    return lead.lead_id


def _make_rep(session) -> uuid.UUID:
    rep = auth.create_rep(session, email=f"{uuid.uuid4()}-rep@example.com", password="testpassword123")
    return rep.rep_id


def test_stages_a_text_with_the_leads_phone_number(db_session):
    lead_id = _make_lead(db_session, phone="+15559990000")

    result = send_lead_text(db_session, lead_id=lead_id, message="Just checking in.")

    assert result["to"] == "+15559990000"
    assert result["stage"] == "awaiting_rep_approval"

    row = db_session.get(ContactHistory, uuid.UUID(result["event_id"]))
    assert row.channel == Channel.TEXT
    assert row.tool == Tool.SEND_LEAD_TEXT
    assert row.content_ref == "Just checking in."


def test_raises_if_lead_has_no_phone_number(db_session):
    lead_id = _make_lead(db_session, phone=None)

    with pytest.raises(ValueError, match="no phone number"):
        send_lead_text(db_session, lead_id=lead_id, message="hi")


def test_raises_for_empty_message(db_session):
    lead_id = _make_lead(db_session)

    with pytest.raises(ValueError, match="message cannot be empty"):
        send_lead_text(db_session, lead_id=lead_id, message="   ")


def test_raises_if_lead_does_not_exist(db_session):
    with pytest.raises(ValueError, match="No lead found"):
        send_lead_text(db_session, lead_id=uuid.uuid4(), message="hi")


def test_execute_returns_none_before_approval(db_session):
    lead_id = _make_lead(db_session)
    staged = send_lead_text(db_session, lead_id=lead_id, message="hi")

    result = execute_send_lead_text(db_session, event_id=staged["event_id"], twilio_client=FakeTwilioClient())

    assert result is None


def test_execute_sends_via_twilio_after_approval(db_session, monkeypatch):
    monkeypatch.setattr(settings, "twilio_from_number", "+15550001111")
    lead_id = _make_lead(db_session, phone="+15557778888")
    rep_id = _make_rep(db_session)
    staged = send_lead_text(db_session, lead_id=lead_id, message="Just checking in on the paperwork.")
    gate.approve(db_session, uuid.UUID(staged["event_id"]), rep_id=rep_id)
    fake_client = FakeTwilioClient(status="queued")

    result = execute_send_lead_text(db_session, event_id=staged["event_id"], twilio_client=fake_client)

    assert result["to"] == "+15557778888"
    assert result["status"] == "queued"
    assert len(fake_client.calls) == 1
    assert fake_client.calls[0]["body"] == "Just checking in on the paperwork."
    assert fake_client.calls[0]["from_"] == "+15550001111"
    assert fake_client.calls[0]["to"] == "+15557778888"

    row = db_session.get(ContactHistory, uuid.UUID(staged["event_id"]))
    assert row.stage == Stage.EXECUTED


def test_execute_raises_if_from_number_not_configured(db_session, monkeypatch):
    monkeypatch.setattr(settings, "twilio_from_number", "")
    lead_id = _make_lead(db_session)
    rep_id = _make_rep(db_session)
    staged = send_lead_text(db_session, lead_id=lead_id, message="hi")
    gate.approve(db_session, uuid.UUID(staged["event_id"]), rep_id=rep_id)

    with pytest.raises(ValueError, match="TWILIO_FROM_NUMBER is empty"):
        execute_send_lead_text(db_session, event_id=staged["event_id"], twilio_client=FakeTwilioClient())

    row = db_session.get(ContactHistory, uuid.UUID(staged["event_id"]))
    assert row.stage == Stage.APPROVED  # not falsely marked executed


def test_execute_is_single_use(db_session, monkeypatch):
    monkeypatch.setattr(settings, "twilio_from_number", "+15550001111")
    lead_id = _make_lead(db_session)
    rep_id = _make_rep(db_session)
    staged = send_lead_text(db_session, lead_id=lead_id, message="hi")
    gate.approve(db_session, uuid.UUID(staged["event_id"]), rep_id=rep_id)

    first = execute_send_lead_text(db_session, event_id=staged["event_id"], twilio_client=FakeTwilioClient())
    second_client = FakeTwilioClient()
    second = execute_send_lead_text(db_session, event_id=staged["event_id"], twilio_client=second_client)

    assert first is not None
    assert second is None
    assert second_client.calls == []


def test_execute_rejects_an_event_id_from_a_different_tool(db_session, monkeypatch):
    monkeypatch.setattr(settings, "twilio_from_number", "+15550001111")
    lead_id = _make_lead(db_session)
    rep_id = _make_rep(db_session)
    other_event = gate.create_draft(
        db_session, lead_id=lead_id, channel=Channel.EMAIL, tool=Tool.SEND_LEAD_EMAIL
    )
    gate.approve(db_session, other_event.event_id, rep_id=rep_id)
    fake_client = FakeTwilioClient()

    result = execute_send_lead_text(db_session, event_id=other_event.event_id, twilio_client=fake_client)

    assert result is None
    assert fake_client.calls == []
    db_session.refresh(other_event)
    assert other_event.stage == Stage.APPROVED  # untouched


def test_registered_under_its_own_name():
    registered = all_tools()
    assert "send_lead_text" in registered
    assert registered["send_lead_text"].handler is send_lead_text
