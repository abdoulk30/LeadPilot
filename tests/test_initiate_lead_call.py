"""Real tests against the real local Postgres — same rationale as
test_gate.py. Covers both halves of this module: the agent-callable
staging tool, and the rep-approval-triggered execute helper.
"""

import uuid

import pytest

from leadpilot import auth, gate
from leadpilot.models.contact_history import Channel, ContactHistory, Stage, Tool
from leadpilot.models.leads import Lead
from leadpilot.tools.base import all_tools
from leadpilot.tools.initiate_lead_call import execute_initiate_lead_call, initiate_lead_call


def _make_lead(session, *, phone: str | None = "+15551234567", name: str = "Test Lead") -> uuid.UUID:
    lead = Lead(display_name=name, primary_phone=phone)
    session.add(lead)
    session.flush()
    return lead.lead_id


def _make_rep(session) -> uuid.UUID:
    rep = auth.create_rep(session, email=f"{uuid.uuid4()}-rep@example.com", password="testpassword123")
    return rep.rep_id


def test_stages_a_call_with_the_leads_phone_number(db_session):
    lead_id = _make_lead(db_session, phone="+15559990000")

    result = initiate_lead_call(db_session, lead_id=lead_id)

    assert result["phone_number"] == "+15559990000"
    assert result["stage"] == "awaiting_rep_approval"

    row = db_session.get(ContactHistory, uuid.UUID(result["event_id"]))
    assert row.channel == Channel.CALL
    assert row.tool == Tool.INITIATE_LEAD_CALL
    assert row.stage == Stage.AWAITING_REP_APPROVAL
    assert row.content_ref == "+15559990000"


def test_raises_if_lead_has_no_phone_number(db_session):
    lead_id = _make_lead(db_session, phone=None)

    with pytest.raises(ValueError, match="no phone number"):
        initiate_lead_call(db_session, lead_id=lead_id)


def test_raises_if_lead_does_not_exist(db_session):
    with pytest.raises(ValueError, match="No lead found"):
        initiate_lead_call(db_session, lead_id=uuid.uuid4())


def test_execute_returns_none_before_approval(db_session):
    lead_id = _make_lead(db_session)
    staged = initiate_lead_call(db_session, lead_id=lead_id)

    result = execute_initiate_lead_call(db_session, event_id=staged["event_id"])

    assert result is None


def test_execute_returns_phone_number_after_approval(db_session):
    lead_id = _make_lead(db_session, phone="+15557778888")
    rep_id = _make_rep(db_session)
    staged = initiate_lead_call(db_session, lead_id=lead_id)

    assert gate.approve(db_session, uuid.UUID(staged["event_id"]), rep_id=rep_id) is True

    result = execute_initiate_lead_call(db_session, event_id=staged["event_id"])

    assert result == "+15557778888"

    row = db_session.get(ContactHistory, uuid.UUID(staged["event_id"]))
    assert row.stage == Stage.EXECUTED


def test_execute_is_single_use(db_session):
    lead_id = _make_lead(db_session)
    rep_id = _make_rep(db_session)
    staged = initiate_lead_call(db_session, lead_id=lead_id)
    gate.approve(db_session, uuid.UUID(staged["event_id"]), rep_id=rep_id)

    first = execute_initiate_lead_call(db_session, event_id=staged["event_id"])
    second = execute_initiate_lead_call(db_session, event_id=staged["event_id"])

    assert first is not None
    assert second is None


def test_execute_rejects_an_event_id_from_a_different_tool(db_session):
    """Guards against execute_initiate_lead_call being called against
    the wrong kind of event — e.g. a send_lead_text draft's event_id
    passed in by mistake.
    """
    lead_id = _make_lead(db_session)
    rep_id = _make_rep(db_session)
    other_event = gate.create_draft(
        db_session, lead_id=lead_id, channel=Channel.TEXT, tool=Tool.SEND_LEAD_TEXT
    )
    gate.approve(db_session, other_event.event_id, rep_id=rep_id)

    result = execute_initiate_lead_call(db_session, event_id=other_event.event_id)

    assert result is None
    db_session.refresh(other_event)
    assert other_event.stage == Stage.APPROVED  # untouched — not silently executed


def test_execute_accepts_string_event_id(db_session):
    lead_id = _make_lead(db_session)
    rep_id = _make_rep(db_session)
    staged = initiate_lead_call(db_session, lead_id=lead_id)
    gate.approve(db_session, uuid.UUID(staged["event_id"]), rep_id=rep_id)

    result = execute_initiate_lead_call(db_session, event_id=staged["event_id"])

    assert result is not None


def test_accepts_string_lead_id(db_session):
    lead_id = _make_lead(db_session)

    result = initiate_lead_call(db_session, lead_id=str(lead_id))

    assert result["stage"] == "awaiting_rep_approval"


def test_registered_under_its_own_name():
    registered = all_tools()
    assert "initiate_lead_call" in registered
    assert registered["initiate_lead_call"].handler is initiate_lead_call
