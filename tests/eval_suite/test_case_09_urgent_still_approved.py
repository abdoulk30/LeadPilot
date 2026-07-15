"""testing/eval-suite.md Case 9 — Urgent callback request is still
rep-approved.

dispatch_slack_handoff only ever calls gate.create_draft, never
gate.try_execute, regardless of message_type (Decision 019: urgency
changes prominence and sort order in the queue, never the approval
gate). Confirms this holds specifically for urgent_callback_request,
not just the other two message types.
"""

import uuid

from leadpilot import auth
from leadpilot.models.contact_history import ContactHistory, Stage
from leadpilot.models.leads import Lead
from leadpilot.tools.dispatch_slack_handoff import dispatch_slack_handoff, execute_dispatch_slack_handoff


class FakeSlackClient:
    def __init__(self):
        self.calls = []

    def chat_postMessage(self, *, channel, text):
        self.calls.append({"channel": channel, "text": text})
        return {"ok": True, "ts": "1.0"}


def _make_rep(session) -> uuid.UUID:
    rep = auth.create_rep(session, email=f"{uuid.uuid4()}-eval-case-9@example.com", password="testpassword123")
    session.flush()
    return rep.rep_id


def _make_lead(session) -> uuid.UUID:
    lead = Lead(display_name="Test Lead")
    session.add(lead)
    session.flush()
    return lead.lead_id


def test_case_9_urgent_handoff_stays_awaiting_approval(db_session, monkeypatch):
    from leadpilot.config import settings

    monkeypatch.setattr(settings, "slack_handoff_channel_ids", "C111,C222,C333")
    lead_id = _make_lead(db_session)

    staged = dispatch_slack_handoff(
        db_session, lead_id=lead_id, message_type="urgent_callback_request",
        message="Lead needs an urgent callback — please call back ASAP.",
    )

    assert staged["stage"] == "awaiting_rep_approval"
    event = db_session.get(ContactHistory, uuid.UUID(staged["event_id"]))
    assert event.stage == Stage.AWAITING_REP_APPROVAL


def test_case_9_urgent_handoff_never_posts_without_approval(db_session, monkeypatch):
    from leadpilot.config import settings

    monkeypatch.setattr(settings, "slack_handoff_channel_ids", "C111,C222,C333")
    lead_id = _make_lead(db_session)
    fake_slack = FakeSlackClient()
    staged = dispatch_slack_handoff(
        db_session, lead_id=lead_id, message_type="urgent_callback_request", message="Call back ASAP.",
    )

    # No rep approval happens in this scenario — the rep hasn't
    # reviewed the queue yet.
    result = execute_dispatch_slack_handoff(db_session, event_id=staged["event_id"], slack_client=fake_slack)

    assert result is None
    assert fake_slack.calls == []  # no Slack message posts to any stakeholder
    event = db_session.get(ContactHistory, uuid.UUID(staged["event_id"]))
    assert event.stage == Stage.AWAITING_REP_APPROVAL
