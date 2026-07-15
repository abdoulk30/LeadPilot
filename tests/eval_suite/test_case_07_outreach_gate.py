"""testing/eval-suite.md Case 7 — Lead outreach gate.

A drafted text/email/call is staged (gate.create_draft, via each
tool's own staging function) and never approved. Each execute_* helper
requires Stage.APPROVED before doing anything real — with no approval,
none of them can ever run, regardless of how much time passes (there's
no separate timeout/cron path that approves on its own).
"""

import uuid

from leadpilot import auth
from leadpilot.models.contact_history import ContactHistory, Stage
from leadpilot.models.leads import Lead
from leadpilot.tools import initiate_lead_call, send_lead_email, send_lead_text


def _make_rep(session) -> uuid.UUID:
    rep = auth.create_rep(session, email=f"{uuid.uuid4()}-eval-case-7@example.com", password="testpassword123")
    session.flush()
    return rep.rep_id


def _make_lead(session, **kwargs) -> uuid.UUID:
    lead = Lead(display_name="Test Lead", **kwargs)
    session.add(lead)
    session.flush()
    return lead.lead_id


def test_case_7_text_never_sent_without_approval(db_session):
    lead_id = _make_lead(db_session, primary_phone="+15550100001")
    staged = send_lead_text.send_lead_text(db_session, lead_id=lead_id, message="Please send your bank statements.")

    result = send_lead_text.execute_send_lead_text(db_session, event_id=staged["event_id"])

    assert result is None  # nothing sent — Stage != APPROVED
    event = db_session.get(ContactHistory, uuid.UUID(staged["event_id"]))
    assert event.stage == Stage.AWAITING_REP_APPROVAL


def test_case_7_email_never_sent_without_approval(db_session):
    lead_id = _make_lead(db_session, primary_email="lead@example.com")
    staged = send_lead_email.send_lead_email(
        db_session, lead_id=lead_id, subject="Missing documents", body="Please send your bank statements."
    )

    result = send_lead_email.execute_send_lead_email(db_session, event_id=staged["event_id"])

    assert result is None
    event = db_session.get(ContactHistory, uuid.UUID(staged["event_id"]))
    assert event.stage == Stage.AWAITING_REP_APPROVAL


def test_case_7_call_never_executed_without_approval(db_session):
    """Same gate, different real effect: no clipboard write, no
    confirmation message — execute_initiate_lead_call returns None,
    same as the text/email cases, when the row was never approved.
    """
    lead_id = _make_lead(db_session, primary_phone="+15550100001")
    staged = initiate_lead_call.initiate_lead_call(db_session, lead_id=lead_id)

    result = initiate_lead_call.execute_initiate_lead_call(db_session, event_id=staged["event_id"])

    assert result is None  # no phone number handed back — nothing to copy
    event = db_session.get(ContactHistory, uuid.UUID(staged["event_id"]))
    assert event.stage == Stage.AWAITING_REP_APPROVAL
