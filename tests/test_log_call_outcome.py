"""Real tests against real local Postgres. initiate_lead_call (Marc's
tool) doesn't exist yet, so tests build the contact_history row
directly against the written contract (architecture/state-schema.md)
instead — that's the whole point of writing the contract down, this
tool should be fully testable without initiate_lead_call's actual
implementation existing.
"""

import uuid

from leadpilot import auth
from leadpilot.models.contact_history import Channel, ContactHistory, Outcome, Stage, Tool
from leadpilot.models.leads import Lead
from leadpilot.tools import log_call_outcome
from leadpilot.tools.registry import load_all_tools

import pytest


def _make_lead(session) -> uuid.UUID:
    lead = Lead(display_name="Test Lead")
    session.add(lead)
    session.flush()
    return lead.lead_id


def _make_rep(session) -> uuid.UUID:
    rep = auth.create_rep(session, email=f"{uuid.uuid4()}-log-call-outcome-test@example.com", password="testpassword123")
    session.flush()
    return rep.rep_id


def _make_event(session, *, tool=Tool.INITIATE_LEAD_CALL, stage=Stage.EXECUTED, outcome=Outcome.PENDING) -> uuid.UUID:
    lead_id = _make_lead(session)
    rep_id = _make_rep(session)
    event = ContactHistory(
        lead_id=lead_id, channel=Channel.CALL, tool=tool, stage=stage, outcome=outcome, rep_id=rep_id
    )
    session.add(event)
    session.flush()
    return event.event_id


def test_registers_as_a_tool():
    tools = load_all_tools()
    assert "log_call_outcome" in tools
    assert tools["log_call_outcome"].handler is log_call_outcome.run


def test_writes_outcome_and_note_for_a_valid_pending_call(db_session):
    event_id = _make_event(db_session)
    result = log_call_outcome.run(db_session, event_id, "answered", note="Interested, will follow up Thursday")

    assert result == {"event_id": str(event_id), "outcome": "answered", "note": "Interested, will follow up Thursday"}

    event = db_session.get(ContactHistory, event_id)
    assert event.outcome == Outcome.ANSWERED
    assert event.note == "Interested, will follow up Thursday"


def test_note_is_optional(db_session):
    event_id = _make_event(db_session)
    result = log_call_outcome.run(db_session, event_id, "no_answer")
    assert result["note"] is None


@pytest.mark.parametrize("outcome", ["answered", "no_answer", "voicemail", "didnt_call"])
def test_accepts_every_rep_reportable_outcome(db_session, outcome):
    event_id = _make_event(db_session)
    result = log_call_outcome.run(db_session, event_id, outcome)
    assert result["outcome"] == outcome


@pytest.mark.parametrize("outcome", ["delivered", "failed", "pending"])
def test_rejects_provider_or_pending_outcomes(db_session, outcome):
    """These are real Outcome enum values, just never ones a rep
    reports about a call themselves — delivered/failed come from
    provider APIs (texts/emails), pending is the state being replaced.
    """
    event_id = _make_event(db_session)
    with pytest.raises(ValueError, match="not a rep-reportable call outcome"):
        log_call_outcome.run(db_session, event_id, outcome)


def test_rejects_unknown_outcome_string(db_session):
    event_id = _make_event(db_session)
    with pytest.raises(ValueError, match="Unknown outcome"):
        log_call_outcome.run(db_session, event_id, "hung_up_on_me")


def test_rejects_no_such_event(db_session):
    with pytest.raises(ValueError, match="No such contact_history event"):
        log_call_outcome.run(db_session, uuid.uuid4(), "answered")


def test_rejects_wrong_tool_type(db_session):
    event_id = _make_event(db_session, tool=Tool.UPDATE_LEAD_SHEET)
    with pytest.raises(ValueError, match="is not an initiate_lead_call entry"):
        log_call_outcome.run(db_session, event_id, "answered")


def test_rejects_not_yet_executed(db_session):
    event_id = _make_event(db_session, stage=Stage.AWAITING_REP_APPROVAL, outcome=None)
    with pytest.raises(ValueError, match="has not been executed yet"):
        log_call_outcome.run(db_session, event_id, "answered")


def test_rejects_call_with_outcome_already_logged(db_session):
    event_id = _make_event(db_session, outcome=Outcome.VOICEMAIL)
    with pytest.raises(ValueError, match="already has a logged outcome"):
        log_call_outcome.run(db_session, event_id, "answered")
