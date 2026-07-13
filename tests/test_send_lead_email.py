"""Real tests against the real local Postgres, plus a fake Gmail
service for the execute half — no live Google credentials or network
access needed to verify the staging/gating/send logic is correct. See
send_lead_email.py's module docstring for why gmail_service is
injectable.
"""

import base64
import email
import uuid

import pytest

from leadpilot import auth, gate
from leadpilot.connectors.google_sheets import RepNotConnectedError
from leadpilot.models.contact_history import Channel, ContactHistory, Stage, Tool
from leadpilot.models.leads import Lead
from leadpilot.tools.base import all_tools
from leadpilot.tools.send_lead_email import execute_send_lead_email, send_lead_email


class FakeGmailService:
    """Mimics googleapiclient's fluent Gmail resource chain
    (.users().messages().send(...).execute()) without a real API call.
    """

    def __init__(self, message_id: str = "msg123"):
        self.sent: list[dict] = []
        self._message_id = message_id

    def users(self):
        return self

    def messages(self):
        return self

    def send(self, *, userId, body):
        self.sent.append({"userId": userId, "body": body})
        return self

    def execute(self):
        return {"id": self._message_id}


def _make_lead(session, *, email: str | None = "lead@example.com", name: str = "Test Lead") -> uuid.UUID:
    lead = Lead(display_name=name, primary_email=email)
    session.add(lead)
    session.flush()
    return lead.lead_id


def _make_rep(session) -> uuid.UUID:
    rep = auth.create_rep(session, email=f"{uuid.uuid4()}-rep@example.com", password="testpassword123")
    return rep.rep_id


def test_stages_an_email_with_the_leads_address(db_session):
    lead_id = _make_lead(db_session, email="jane@acme.com")

    result = send_lead_email(db_session, lead_id=lead_id, subject="Following up", body="Just checking in.")

    assert result["to"] == "jane@acme.com"
    assert result["subject"] == "Following up"
    assert result["stage"] == "awaiting_rep_approval"

    row = db_session.get(ContactHistory, uuid.UUID(result["event_id"]))
    assert row.channel == Channel.EMAIL
    assert row.tool == Tool.SEND_LEAD_EMAIL


def test_raises_if_lead_has_no_email(db_session):
    lead_id = _make_lead(db_session, email=None)

    with pytest.raises(ValueError, match="no email address"):
        send_lead_email(db_session, lead_id=lead_id, subject="Hi", body="Hi there.")


def test_raises_for_empty_subject(db_session):
    lead_id = _make_lead(db_session)

    with pytest.raises(ValueError, match="subject cannot be empty"):
        send_lead_email(db_session, lead_id=lead_id, subject="  ", body="Hi there.")


def test_raises_for_empty_body(db_session):
    lead_id = _make_lead(db_session)

    with pytest.raises(ValueError, match="body cannot be empty"):
        send_lead_email(db_session, lead_id=lead_id, subject="Hi", body="   ")


def test_raises_if_lead_does_not_exist(db_session):
    with pytest.raises(ValueError, match="No lead found"):
        send_lead_email(db_session, lead_id=uuid.uuid4(), subject="Hi", body="Hi there.")


def test_execute_returns_none_before_approval(db_session):
    lead_id = _make_lead(db_session)
    staged = send_lead_email(db_session, lead_id=lead_id, subject="Hi", body="Hi there.")

    result = execute_send_lead_email(db_session, event_id=staged["event_id"], gmail_service=FakeGmailService())

    assert result is None


def test_execute_sends_the_right_content_after_approval(db_session):
    lead_id = _make_lead(db_session, email="jane@acme.com")
    rep_id = _make_rep(db_session)
    staged = send_lead_email(
        db_session, lead_id=lead_id, subject="Following up", body="Just checking in on the paperwork."
    )
    gate.approve(db_session, uuid.UUID(staged["event_id"]), rep_id=rep_id)
    fake_service = FakeGmailService(message_id="msg-abc-123")

    result = execute_send_lead_email(db_session, event_id=staged["event_id"], gmail_service=fake_service)

    assert result["message_id"] == "msg-abc-123"
    assert result["to"] == "jane@acme.com"
    assert len(fake_service.sent) == 1

    raw = fake_service.sent[0]["body"]["raw"]
    decoded = base64.urlsafe_b64decode(raw.encode())
    parsed = email.message_from_bytes(decoded)
    assert parsed["to"] == "jane@acme.com"
    assert parsed["subject"] == "Following up"
    assert parsed.get_payload() == "Just checking in on the paperwork."

    row = db_session.get(ContactHistory, uuid.UUID(staged["event_id"]))
    assert row.stage == Stage.EXECUTED


def test_execute_is_single_use(db_session):
    lead_id = _make_lead(db_session)
    rep_id = _make_rep(db_session)
    staged = send_lead_email(db_session, lead_id=lead_id, subject="Hi", body="Hi there.")
    gate.approve(db_session, uuid.UUID(staged["event_id"]), rep_id=rep_id)

    first = execute_send_lead_email(db_session, event_id=staged["event_id"], gmail_service=FakeGmailService())
    second_service = FakeGmailService()
    second = execute_send_lead_email(db_session, event_id=staged["event_id"], gmail_service=second_service)

    assert first is not None
    assert second is None
    assert second_service.sent == []


def test_execute_rejects_an_event_id_from_a_different_tool(db_session):
    lead_id = _make_lead(db_session)
    rep_id = _make_rep(db_session)
    other_event = gate.create_draft(
        db_session, lead_id=lead_id, channel=Channel.TEXT, tool=Tool.SEND_LEAD_TEXT
    )
    gate.approve(db_session, other_event.event_id, rep_id=rep_id)
    fake_service = FakeGmailService()

    result = execute_send_lead_email(db_session, event_id=other_event.event_id, gmail_service=fake_service)

    assert result is None
    assert fake_service.sent == []
    db_session.refresh(other_event)
    assert other_event.stage == Stage.APPROVED  # untouched


def test_execute_raises_if_approving_rep_never_connected_google(db_session):
    """No gmail_service injected and the approving rep has no
    rep_google_credentials row — should fail loudly (RepNotConnectedError),
    not silently skip the send.
    """
    lead_id = _make_lead(db_session)
    rep_id = _make_rep(db_session)  # never goes through Google OAuth
    staged = send_lead_email(db_session, lead_id=lead_id, subject="Hi", body="Hi there.")
    gate.approve(db_session, uuid.UUID(staged["event_id"]), rep_id=rep_id)

    with pytest.raises(RepNotConnectedError):
        execute_send_lead_email(db_session, event_id=staged["event_id"])


def test_registered_under_its_own_name():
    registered = all_tools()
    assert "send_lead_email" in registered
    assert registered["send_lead_email"].handler is send_lead_email
