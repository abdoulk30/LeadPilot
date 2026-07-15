"""testing/eval-suite.md Case 10 — Call outcome closes the loop.

The contact-history/outcome half is fully real and verified here:
log_call_outcome flips outcome from pending to no_answer, and
queue_builder's interim rank heuristic (Decision 036 A9, the
deterministic stand-in until Step 4's agent exists) correctly reflects
that change in its rank_reason string — proving Rank 3 logic actually
has the data it needs, and doesn't when the rep never calls
log_call_outcome (the negative case).

Still blocked: "the agent stages an explicit Text or Email follow-up
for that lead" describes the *agent* auto-drafting something, which
needs Step 4's loop to exist — nothing in this codebase auto-drafts
outreach today, only the agent (once built) will.
"""

import uuid

from leadpilot import auth, gate, queue_builder
from leadpilot.models.contact_history import ContactHistory, Outcome
from leadpilot.models.leads import Lead
from leadpilot.tools import initiate_lead_call, log_call_outcome


def _make_rep(session) -> uuid.UUID:
    rep = auth.create_rep(session, email=f"{uuid.uuid4()}-eval-case-10@example.com", password="testpassword123")
    session.flush()
    return rep.rep_id


def _make_lead(session, **kwargs) -> uuid.UUID:
    lead = Lead(display_name="John Doe", **kwargs)
    session.add(lead)
    session.flush()
    return lead.lead_id


def _place_and_execute_call(session, rep_id, lead_id) -> uuid.UUID:
    staged = initiate_lead_call.initiate_lead_call(session, lead_id=lead_id)
    event_id = uuid.UUID(staged["event_id"])
    gate.approve(session, event_id, rep_id=rep_id)
    initiate_lead_call.execute_initiate_lead_call(session, event_id=event_id)
    return event_id


def test_case_10_positive_outcome_logged_closes_the_loop(db_session):
    rep_id = _make_rep(db_session)
    lead_id = _make_lead(db_session, primary_phone="+15550100001")
    event_id = _place_and_execute_call(db_session, rep_id, lead_id)

    # Before log_call_outcome: outcome is pending, and Rank 3's
    # follow-up rule has nothing to act on for this lead.
    queue = queue_builder.build_queue(db_session, rep_id)
    item = next(i for i in queue if i["lead_id"] == str(lead_id))
    assert item["rank"] == 3
    assert "not logged yet" in item["rank_reason"]

    # The rep reports the outcome.
    log_call_outcome.run(db_session, event_id, "no_answer")

    event = db_session.get(ContactHistory, event_id)
    assert event.outcome == Outcome.NO_ANSWER

    # Rank 3 logic now has the data it needs — a distinct reason for
    # "unanswered, needs multi-channel follow-up" versus "not logged yet".
    queue = queue_builder.build_queue(db_session, rep_id)
    item = next(i for i in queue if i["lead_id"] == str(lead_id))
    assert item["rank"] == 3
    assert "unanswered" in item["rank_reason"]
    assert "needs multi-channel follow-up" in item["rank_reason"]


def test_case_10_negative_no_outcome_logged_stays_pending(db_session):
    """The rep never calls log_call_outcome — outcome stays pending,
    and the unanswered-call follow-up rule cannot fire for this lead
    (it can only distinguish "not logged yet" from a real outcome).
    """
    rep_id = _make_rep(db_session)
    lead_id = _make_lead(db_session, primary_phone="+15550100002")
    event_id = _place_and_execute_call(db_session, rep_id, lead_id)

    event = db_session.get(ContactHistory, event_id)
    assert event.outcome == Outcome.PENDING

    queue = queue_builder.build_queue(db_session, rep_id)
    item = next(i for i in queue if i["lead_id"] == str(lead_id))
    assert item["rank"] == 3
    assert "call outcome not logged yet" in item["rank_reason"]
    assert "unanswered" not in item["rank_reason"]
