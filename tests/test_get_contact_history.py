"""Real tests against the real local Postgres — same rationale as
test_gate.py: this tool's correctness is about reading real rows back
correctly, not logic a mock would exercise meaningfully.
"""

import uuid
from datetime import datetime, timedelta, timezone

from leadpilot import auth
from leadpilot.models.contact_history import Channel, ContactHistory, Outcome, Stage, Tool
from leadpilot.models.leads import Lead
from leadpilot.tools.base import all_tools
from leadpilot.tools.get_contact_history import get_contact_history


def _make_lead(session, name: str = "Test Lead") -> uuid.UUID:
    lead = Lead(display_name=name)
    session.add(lead)
    session.flush()
    return lead.lead_id


def _make_rep(session) -> uuid.UUID:
    rep = auth.create_rep(session, email=f"{uuid.uuid4()}-rep@example.com", password="testpassword123")
    return rep.rep_id


def _make_event(session, *, lead_id, channel, tool, stage=Stage.EXECUTED, outcome=None, rep_id=None, **kw):
    event = ContactHistory(
        lead_id=lead_id, channel=channel, tool=tool, stage=stage, outcome=outcome, rep_id=rep_id, **kw
    )
    session.add(event)
    session.flush()
    return event


def test_empty_history_for_lead_with_no_events(db_session):
    lead_id = _make_lead(db_session)
    assert get_contact_history(db_session, lead_id=lead_id) == []


def test_only_returns_events_for_the_requested_lead(db_session):
    lead_a = _make_lead(db_session, "Lead A")
    lead_b = _make_lead(db_session, "Lead B")
    _make_event(db_session, lead_id=lead_a, channel=Channel.TEXT, tool=Tool.SEND_LEAD_TEXT)
    _make_event(db_session, lead_id=lead_b, channel=Channel.EMAIL, tool=Tool.SEND_LEAD_EMAIL)

    result = get_contact_history(db_session, lead_id=lead_a)

    assert len(result) == 1
    assert result[0]["channel"] == "text"


def test_most_recent_event_first(db_session):
    lead_id = _make_lead(db_session)
    now = datetime.now(timezone.utc)
    older = _make_event(db_session, lead_id=lead_id, channel=Channel.EMAIL, tool=Tool.SEND_LEAD_EMAIL)
    older.timestamp = now - timedelta(days=2)
    newer = _make_event(db_session, lead_id=lead_id, channel=Channel.TEXT, tool=Tool.SEND_LEAD_TEXT)
    newer.timestamp = now
    db_session.flush()

    result = get_contact_history(db_session, lead_id=lead_id)

    assert [row["event_id"] for row in result] == [str(newer.event_id), str(older.event_id)]


def test_pending_call_outcome_is_null_not_missing(db_session):
    """A just-approved call has outcome=PENDING until log_call_outcome
    closes the loop (Decision 032, architecture/state-schema.md
    "Outcome visibility") — confirms that comes through as the string
    'pending', not None/absent, so callers can tell "no outcome yet"
    apart from "not a call."
    """
    lead_id = _make_lead(db_session)
    _make_event(
        db_session,
        lead_id=lead_id,
        channel=Channel.CALL,
        tool=Tool.INITIATE_LEAD_CALL,
        stage=Stage.EXECUTED,
        outcome=Outcome.PENDING,
    )

    result = get_contact_history(db_session, lead_id=lead_id)

    assert result[0]["outcome"] == "pending"


def test_reports_rep_attribution_and_note(db_session):
    lead_id = _make_lead(db_session)
    rep_id = _make_rep(db_session)
    _make_event(
        db_session,
        lead_id=lead_id,
        channel=Channel.CALL,
        tool=Tool.INITIATE_LEAD_CALL,
        stage=Stage.EXECUTED,
        outcome=Outcome.NO_ANSWER,
        rep_id=rep_id,
        note="Left voicemail, will retry Thursday",
    )

    result = get_contact_history(db_session, lead_id=lead_id)

    assert result[0]["rep_id"] == str(rep_id)
    assert result[0]["note"] == "Left voicemail, will retry Thursday"
    assert result[0]["outcome"] == "no_answer"


def test_accepts_string_lead_id_same_as_the_sdk_tool_calling_loop_would_pass(db_session):
    lead_id = _make_lead(db_session)
    _make_event(db_session, lead_id=lead_id, channel=Channel.TEXT, tool=Tool.SEND_LEAD_TEXT)

    result = get_contact_history(db_session, lead_id=str(lead_id))

    assert len(result) == 1


def test_registered_under_its_own_name():
    """The @tool(...) decorator runs at import time (base.py's
    docstring: "the registry is process-global by design"), and this
    file already imported get_contact_history above — so it should
    already be registered, no reset/reload needed to prove it.
    """
    registered = all_tools()
    assert "get_contact_history" in registered
    assert registered["get_contact_history"].handler is get_contact_history
