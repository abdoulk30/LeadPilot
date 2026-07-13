"""log_call_outcome — PRD v1.05 §3a/§3f. The rep reporting what actually
happened on a call they placed themselves (answered / no answer /
voicemail / didn't call), against that call's own entry in the
contact-history log. Rep-initiated, writes only to LeadPilot's internal
log, no external system on the other side of it — so unlike
update_lead_sheet/initiate_lead_call/etc., this does not go through the
approval gate (Decision 021). Until a rep calls this, the entry sits at
outcome=PENDING and Rank 3's unanswered-call follow-up rule has nothing
to act on for that lead (PRD Eval Case 10).

Exact row-matching contract (architecture/state-schema.md, written down
2026-07-12 since initiate_lead_call and log_call_outcome are built by
different people — Decision 032): takes event_id directly, the same
contact_history.event_id the rep is looking at in their queue — never a
lead_id-based lookup, which would be ambiguous (which of possibly
several past calls for this lead is "the" pending one?). Before writing,
verifies all three of tool == INITIATE_LEAD_CALL, stage == EXECUTED,
outcome == PENDING; rejects otherwise rather than silently overwriting a
row that isn't a fresh, pending call handoff. This is the entire
coordination surface with initiate_lead_call (Marc's tool) — as long as
that tool creates its row with tool=INITIATE_LEAD_CALL and, once
executed, outcome=PENDING (both already fixed by the existing Tool/
Outcome enums in contact_history.py), this tool can be built and tested
independently against the contract, without initiate_lead_call's actual
implementation existing yet.
"""

import uuid

from sqlalchemy.orm import Session

from leadpilot.models.contact_history import ContactHistory, Outcome, Stage, Tool
from leadpilot.tools.base import tool

# DELIVERED/FAILED are provider-reported outcomes for texts/emails, not
# something a rep reports about a call; PENDING is the state being
# replaced, never a value the rep asserts. A rep only ever reports one
# of these four about a call they placed themselves.
REP_REPORTABLE_OUTCOMES = {Outcome.ANSWERED, Outcome.NO_ANSWER, Outcome.VOICEMAIL, Outcome.DIDNT_CALL}


@tool(
    name="log_call_outcome",
    description=(
        "Records the rep-reported outcome of a previously staged initiate_lead_call — answered, "
        "no_answer, voicemail, or didnt_call, plus an optional note — against that call's own "
        "contact-history entry. No approval gate: this is the rep directly reporting a fact after "
        "making the call themselves, not an agent-drafted action with an external effect."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "event_id": {"type": "string", "description": "The contact_history event_id of the executed call"},
            "outcome": {
                "type": "string",
                "enum": [o.value for o in REP_REPORTABLE_OUTCOMES],
                "description": "What actually happened on the call",
            },
            "note": {"type": "string", "description": "Optional free-text note from the rep"},
        },
        "required": ["event_id", "outcome"],
    },
)
def run(session: Session, event_id: uuid.UUID, outcome: str, note: str | None = None) -> dict:
    event = session.get(ContactHistory, event_id)
    if event is None:
        raise ValueError(f"No such contact_history event: {event_id}")
    if event.tool != Tool.INITIATE_LEAD_CALL:
        raise ValueError(f"Event {event_id} is not an initiate_lead_call entry (tool={event.tool})")
    if event.stage != Stage.EXECUTED:
        raise ValueError(f"Event {event_id} has not been executed yet (stage={event.stage})")
    if event.outcome != Outcome.PENDING:
        raise ValueError(f"Event {event_id} already has a logged outcome ({event.outcome})")

    try:
        outcome_enum = Outcome(outcome)
    except ValueError:
        raise ValueError(f"Unknown outcome: {outcome!r}") from None
    if outcome_enum not in REP_REPORTABLE_OUTCOMES:
        raise ValueError(
            f"{outcome!r} is not a rep-reportable call outcome — must be one of "
            f"{sorted(o.value for o in REP_REPORTABLE_OUTCOMES)}"
        )

    event.outcome = outcome_enum
    if note is not None:
        event.note = note
    session.commit()

    return {"event_id": str(event.event_id), "outcome": outcome_enum.value, "note": event.note}
