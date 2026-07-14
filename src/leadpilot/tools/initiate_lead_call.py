"""initiate_lead_call — Step 2 tool (Marc, Group B, Decision 032).

Stages a recommended call (execution-gating rule, PRD v1.05 3a): this
tool only ever calls gate.create_draft, never gate.try_execute — that
authorization is the rep's approval action, wired in Step 3, not
something this tool grants itself. Per Decision 016, there is no
telephony API anywhere in this project: the "real effect" once
approved is a local clipboard write of the lead's phone number plus a
confirmation message, not a dialed call — see
leadpilot-docs/testing/known-issues-log.md Issue 001 (Google Voice has
no API and is not used by any tool).

execute_initiate_lead_call() below is the other half of that flow —
not agent-callable (the agent only ever stages), meant to be called by
whatever Step 3 endpoint handles the rep's "Approve" click on a
pending call. Kept in this module rather than deferred entirely to
Step 3 since it's a small wrapper around gate.try_execute() that
belongs next to the tool it executes, and since the event_id it needs
is exactly what initiate_lead_call() below hands back — matches the
event_id-based log_call_outcome contract from Decision 032, same
identifier, no separate lookup either tool has to invent.
"""

import uuid

from sqlalchemy.orm import Session

from leadpilot import gate
from leadpilot.models.contact_history import Channel, ContactHistory, Outcome, Tool
from leadpilot.models.leads import Lead
from leadpilot.tools.base import tool


@tool(
    name="initiate_lead_call",
    description=(
        "Stages a recommended call to a lead. Does not call any telephony "
        "API and does not dial anything — on rep approval (handled "
        "elsewhere, never by this tool), the real effect is copying the "
        "lead's phone number to the rep's clipboard and showing a "
        "confirmation message; the rep places the call manually in "
        "whatever calling app they use."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "lead_id": {
                "type": "string",
                "format": "uuid",
                "description": "The canonical lead_id (post-dedup) to stage a call for.",
            }
        },
        "required": ["lead_id"],
    },
)
def initiate_lead_call(session: Session, *, lead_id: uuid.UUID | str) -> dict:
    """Looks up the lead's own phone number (rather than trusting a
    caller-supplied one, so a stale number in the agent's context can't
    silently diverge from what's actually on file) and stages a draft
    call event. Raises ValueError if the lead doesn't exist or has no
    phone number on file — nothing to call, so nothing to stage.
    """
    if isinstance(lead_id, str):
        lead_id = uuid.UUID(lead_id)

    lead = session.get(Lead, lead_id)
    if lead is None:
        raise ValueError(f"No lead found for lead_id={lead_id}")
    if not lead.primary_phone:
        raise ValueError(f"Lead {lead_id} has no phone number on file — nothing to call")

    event = gate.create_draft(
        session,
        lead_id=lead_id,
        channel=Channel.CALL,
        tool=Tool.INITIATE_LEAD_CALL,
        content_ref=lead.primary_phone,
    )

    return {
        "event_id": str(event.event_id),
        "stage": event.stage.value,
        "phone_number": lead.primary_phone,
    }


def execute_initiate_lead_call(session: Session, *, event_id: uuid.UUID | str) -> str | None:
    """Called by Step 3's approval endpoint, never by the agent. Flips
    the staged row to EXECUTED via the same single atomic conditional
    update every other tool's execution uses (gate.try_execute,
    Decision 021), then hands back the phone number for the frontend
    to copy to the clipboard — or None if the row wasn't actually in
    an approved, executable state (already executed, rejected,
    expired, or a stale/mismatched event_id), in which case nothing
    should be copied and no confirmation shown.
    """
    if isinstance(event_id, str):
        event_id = uuid.UUID(event_id)

    event = session.get(ContactHistory, event_id)
    if event is None or event.channel != Channel.CALL or event.tool != Tool.INITIATE_LEAD_CALL:
        return None

    if not gate.try_execute(session, event_id):
        return None

    # The executed-call ↔ log_call_outcome contract (Decision 032,
    # architecture/state-schema.md "Outcome visibility"): an executed
    # call sits at outcome=PENDING until the rep reports back via
    # log_call_outcome, which refuses rows at any other outcome. This
    # was documented ("once executed, outcome=PENDING") but nothing
    # actually set it — log_call_outcome's tests built their rows with
    # PENDING pre-set, masking the gap; Step 3's approve flow is the
    # first real caller to hit it end-to-end.
    event.outcome = Outcome.PENDING
    session.flush()

    return event.content_ref
