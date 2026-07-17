"""Real tests against the real local Postgres, plus a fake Slack
client for the execute half — no live Slack token or network access
needed to verify the staging/gating/fan-out logic is correct. See
dispatch_slack_handoff.py's module docstring for why slack_client is
injectable.
"""

import uuid

import pytest

from leadpilot import auth, gate
from leadpilot.config import settings
from leadpilot.models.contact_history import Channel, ContactHistory, MessageType, Stage, Tool
from leadpilot.models.leads import Lead
from leadpilot.tools.base import all_tools
from leadpilot.tools.dispatch_slack_handoff import (
    dispatch_slack_handoff,
    execute_dispatch_slack_handoff,
)


class FakeSlackClient:
    """Records every chat_postMessage call instead of hitting the real
    Slack API. Returns a canned success response by default.
    """

    def __init__(self, ok: bool = True):
        self.calls: list[dict] = []
        self._ok = ok

    def chat_postMessage(self, *, channel: str, text: str) -> dict:
        self.calls.append({"channel": channel, "text": text})
        return {"ok": self._ok, "ts": f"169000000{len(self.calls)}.000100"}


def _make_lead(session, name: str = "Test Lead") -> uuid.UUID:
    lead = Lead(display_name=name)
    session.add(lead)
    session.flush()
    return lead.lead_id


def _make_rep(session) -> uuid.UUID:
    rep = auth.create_rep(session, email=f"{uuid.uuid4()}-rep@example.com", password="testpassword123")
    return rep.rep_id


def test_stages_a_handoff_with_message_type_and_content(db_session, monkeypatch):
    monkeypatch.setattr(settings, "slack_handoff_channel_ids", "C111")
    lead_id = _make_lead(db_session)

    result = dispatch_slack_handoff(
        db_session, lead_id=lead_id, message_type="completion_handoff", message="Docs are in, ready to close."
    )

    assert result["message_type"] == "completion_handoff"
    assert result["stage"] == "awaiting_rep_approval"

    row = db_session.get(ContactHistory, uuid.UUID(result["event_id"]))
    assert row.channel == Channel.SLACK_HANDOFF
    assert row.tool == Tool.DISPATCH_SLACK_HANDOFF
    assert row.content_ref == "Docs are in, ready to close."
    assert row.message_type == MessageType.COMPLETION_HANDOFF


def test_raises_for_invalid_message_type(db_session, monkeypatch):
    monkeypatch.setattr(settings, "slack_handoff_channel_ids", "C111")
    lead_id = _make_lead(db_session)

    with pytest.raises(ValueError, match="Unrecognized message_type"):
        dispatch_slack_handoff(db_session, lead_id=lead_id, message_type="urgent_email", message="hi")


def test_raises_for_empty_message(db_session, monkeypatch):
    monkeypatch.setattr(settings, "slack_handoff_channel_ids", "C111")
    lead_id = _make_lead(db_session)

    with pytest.raises(ValueError, match="cannot be empty"):
        dispatch_slack_handoff(db_session, lead_id=lead_id, message_type="info_request", message="   ")


def test_raises_if_no_channels_configured(db_session, monkeypatch):
    monkeypatch.setattr(settings, "slack_handoff_channel_ids", "")
    lead_id = _make_lead(db_session)

    with pytest.raises(ValueError, match="SLACK_HANDOFF_CHANNEL_IDS is empty"):
        dispatch_slack_handoff(db_session, lead_id=lead_id, message_type="info_request", message="hi")


def test_execute_returns_none_before_approval(db_session, monkeypatch):
    monkeypatch.setattr(settings, "slack_handoff_channel_ids", "C111")
    lead_id = _make_lead(db_session)
    staged = dispatch_slack_handoff(db_session, lead_id=lead_id, message_type="info_request", message="hi")

    result = execute_dispatch_slack_handoff(
        db_session, event_id=staged["event_id"], slack_client=FakeSlackClient()
    )

    assert result is None


def test_execute_raises_if_channels_emptied_after_staging(db_session, monkeypatch):
    """A gap actually found live: seed_demo_data.py stages drafts under
    an in-process settings override, but the real server process reads
    SLACK_HANDOFF_CHANNEL_IDS fresh from .env.local — so a draft can be
    staged with channels configured, then approved in a process where
    they're empty. Before this test, that fell through to '0 of 0
    deliveries', shown as success. Must raise instead, leaving the row
    APPROVED (config failure, not consumed) rather than lying about
    having sent anything.
    """
    monkeypatch.setattr(settings, "slack_handoff_channel_ids", "C111")
    lead_id = _make_lead(db_session)
    rep_id = _make_rep(db_session)
    staged = dispatch_slack_handoff(db_session, lead_id=lead_id, message_type="info_request", message="hi")
    gate.approve(db_session, uuid.UUID(staged["event_id"]), rep_id=rep_id)

    monkeypatch.setattr(settings, "slack_handoff_channel_ids", "")
    fake_client = FakeSlackClient()

    with pytest.raises(RuntimeError, match="SLACK_HANDOFF_CHANNEL_IDS is empty"):
        execute_dispatch_slack_handoff(db_session, event_id=staged["event_id"], slack_client=fake_client)

    assert fake_client.calls == []
    row = db_session.get(ContactHistory, uuid.UUID(staged["event_id"]))
    assert row.stage == Stage.APPROVED


def test_execute_posts_to_every_configured_channel_after_approval(db_session, monkeypatch):
    monkeypatch.setattr(settings, "slack_handoff_channel_ids", "C111,C222,C333")
    lead_id = _make_lead(db_session)
    rep_id = _make_rep(db_session)
    staged = dispatch_slack_handoff(
        db_session, lead_id=lead_id, message_type="urgent_callback_request", message="Call back ASAP"
    )
    gate.approve(db_session, uuid.UUID(staged["event_id"]), rep_id=rep_id)
    fake_client = FakeSlackClient()

    result = execute_dispatch_slack_handoff(db_session, event_id=staged["event_id"], slack_client=fake_client)

    assert len(fake_client.calls) == 3
    assert {c["channel"] for c in fake_client.calls} == {"C111", "C222", "C333"}
    assert all(c["text"] == "Call back ASAP" for c in fake_client.calls)
    assert len(result["deliveries"]) == 3
    assert all(d["ok"] for d in result["deliveries"])
    assert result["message_type"] == "urgent_callback_request"

    row = db_session.get(ContactHistory, uuid.UUID(staged["event_id"]))
    assert row.stage == Stage.EXECUTED


def test_no_exception_for_urgent_messages_still_requires_approval(db_session, monkeypatch):
    """Decision 019: urgency is not a reason to skip rep approval, even
    for an internal-only recipient. Confirms an urgent_callback_request
    still sits at AWAITING_REP_APPROVAL and posts nothing until
    approved, same as any other message type.
    """
    monkeypatch.setattr(settings, "slack_handoff_channel_ids", "C111")
    lead_id = _make_lead(db_session)
    staged = dispatch_slack_handoff(
        db_session, lead_id=lead_id, message_type="urgent_callback_request", message="Call back ASAP"
    )
    fake_client = FakeSlackClient()

    result = execute_dispatch_slack_handoff(db_session, event_id=staged["event_id"], slack_client=fake_client)

    assert result is None
    assert fake_client.calls == []


def test_execute_is_single_use(db_session, monkeypatch):
    monkeypatch.setattr(settings, "slack_handoff_channel_ids", "C111")
    lead_id = _make_lead(db_session)
    rep_id = _make_rep(db_session)
    staged = dispatch_slack_handoff(db_session, lead_id=lead_id, message_type="info_request", message="hi")
    gate.approve(db_session, uuid.UUID(staged["event_id"]), rep_id=rep_id)

    first = execute_dispatch_slack_handoff(
        db_session, event_id=staged["event_id"], slack_client=FakeSlackClient()
    )
    second_client = FakeSlackClient()
    second = execute_dispatch_slack_handoff(db_session, event_id=staged["event_id"], slack_client=second_client)

    assert first is not None
    assert second is None
    assert second_client.calls == []


def test_execute_rejects_an_event_id_from_a_different_tool(db_session, monkeypatch):
    monkeypatch.setattr(settings, "slack_handoff_channel_ids", "C111")
    lead_id = _make_lead(db_session)
    rep_id = _make_rep(db_session)
    other_event = gate.create_draft(
        db_session, lead_id=lead_id, channel=Channel.TEXT, tool=Tool.SEND_LEAD_TEXT
    )
    gate.approve(db_session, other_event.event_id, rep_id=rep_id)
    fake_client = FakeSlackClient()

    result = execute_dispatch_slack_handoff(db_session, event_id=other_event.event_id, slack_client=fake_client)

    assert result is None
    assert fake_client.calls == []
    db_session.refresh(other_event)
    assert other_event.stage == Stage.APPROVED  # untouched


def test_registered_under_its_own_name():
    registered = all_tools()
    assert "dispatch_slack_handoff" in registered
    assert registered["dispatch_slack_handoff"].handler is dispatch_slack_handoff
