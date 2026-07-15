"""testing/eval-suite.md Case 8 — Lead call clipboard handoff.

execute_initiate_lead_call structurally cannot make an external call —
there is no telephony client anywhere in its code path to inject or
mock (Decision 016: no telephony API exists in this project at all).
Approving hands back the lead's own phone number for the frontend to
copy; nothing else happens server-side.
"""

import uuid

from leadpilot import auth, gate
from leadpilot.models.contact_history import ContactHistory, Outcome, Stage
from leadpilot.models.leads import Lead
from leadpilot.tools import initiate_lead_call


def _make_rep(session) -> uuid.UUID:
    rep = auth.create_rep(session, email=f"{uuid.uuid4()}-eval-case-8@example.com", password="testpassword123")
    session.flush()
    return rep.rep_id


def _make_lead(session, **kwargs) -> uuid.UUID:
    lead = Lead(display_name="John Doe", **kwargs)
    session.add(lead)
    session.flush()
    return lead.lead_id


def test_case_8_approved_call_returns_the_phone_number_to_copy(db_session):
    rep_id = _make_rep(db_session)
    lead_id = _make_lead(db_session, primary_phone="+15550100001")
    staged = initiate_lead_call.initiate_lead_call(db_session, lead_id=lead_id)
    event_id = uuid.UUID(staged["event_id"])
    assert gate.approve(db_session, event_id, rep_id=rep_id) is True

    # The interface's confirmation ("Copied — ready to call John Doe")
    # is built from this return value plus the lead's own name — the
    # tool itself only needs to hand back the number.
    phone_number = initiate_lead_call.execute_initiate_lead_call(db_session, event_id=event_id)

    assert phone_number == "+15550100001"

    event = db_session.get(ContactHistory, event_id)
    assert event.stage == Stage.EXECUTED
    # Sets up log_call_outcome's contract (Decision 032) — a real call
    # is now "pending" a rep-reported outcome, not stuck as EXECUTED
    # with nothing further ever expected.
    assert event.outcome == Outcome.PENDING


def test_case_8_execute_initiate_lead_call_has_no_external_client_to_call():
    """Structural proof, not a runtime assertion: the function takes no
    client/service parameter of any kind, unlike every other execute_*
    function in this codebase (twilio_client, gmail_service,
    slack_client) — there's nothing to inject because nothing external
    is ever called. Google Voice (or any calling app) is never opened,
    filled, or otherwise interacted with by this code.
    """
    import inspect

    params = inspect.signature(initiate_lead_call.execute_initiate_lead_call).parameters
    assert set(params) == {"session", "event_id"}
