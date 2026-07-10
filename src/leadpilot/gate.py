"""The approval gate — Decision 021's mechanism, as real code.

No separate token object. A staged action's row in contact_history
moves through drafted -> awaiting_rep_approval -> approved -> executed
(or rejected/expired). Each transition here is a single atomic
conditional UPDATE that only succeeds if the row is still in the
expected prior stage. That's what makes "single-use" true: if two
concurrent requests both try to execute the same event, only one
UPDATE can match `stage = 'approved'` — the other sees zero rows
affected and does nothing. See architecture/state-schema.md.
"""

import uuid

from sqlalchemy import update
from sqlalchemy.orm import Session

from leadpilot.models.contact_history import Channel, ContactHistory, Stage, Tool


def create_draft(
    session: Session,
    *,
    lead_id: uuid.UUID,
    channel: Channel,
    tool: Tool,
    content_ref: str | None = None,
    stage: Stage = Stage.AWAITING_REP_APPROVAL,
) -> ContactHistory:
    """Stage a new action. Defaults straight to AWAITING_REP_APPROVAL,
    matching the system prompt's OUTPUT FORMAT (PRD v1.04 3b), which
    only ever shows the rep actions already at that stage. `stage`
    is exposed as a parameter in case Step 2 tool code needs the
    earlier `drafted` state for its own validation step first.
    """
    event = ContactHistory(
        lead_id=lead_id,
        channel=channel,
        tool=tool,
        content_ref=content_ref,
        stage=stage,
    )
    session.add(event)
    session.flush()
    return event


def approve(session: Session, event_id: uuid.UUID, rep_id: str) -> bool:
    """The rep's 'Approve' click. Flips exactly one row from
    awaiting_rep_approval -> approved. Returns False (no-op) if the
    row isn't in that state — already approved, rejected, expired, or
    doesn't exist.
    """
    result = session.execute(
        update(ContactHistory)
        .where(
            ContactHistory.event_id == event_id,
            ContactHistory.stage == Stage.AWAITING_REP_APPROVAL,
        )
        .values(stage=Stage.APPROVED, rep_id=rep_id)
    )
    return result.rowcount == 1


def try_execute(session: Session, event_id: uuid.UUID) -> bool:
    """The single atomic conditional update Decision 021 describes.
    Only the caller that flips this row (approved -> executed) is
    cleared to actually run the tool's real effect (send the text,
    post to Slack, write the sheet, copy the number to the clipboard).
    Everyone else — including a concurrent duplicate request — gets
    False and must not perform the real effect.
    """
    result = session.execute(
        update(ContactHistory)
        .where(
            ContactHistory.event_id == event_id,
            ContactHistory.stage == Stage.APPROVED,
        )
        .values(stage=Stage.EXECUTED)
    )
    return result.rowcount == 1


def reject(session: Session, event_id: uuid.UUID, rep_id: str) -> bool:
    """The rep declines a still-pending or approved-but-not-yet-executed
    action. Won't touch a row that's already executed, rejected, or
    expired.
    """
    result = session.execute(
        update(ContactHistory)
        .where(
            ContactHistory.event_id == event_id,
            ContactHistory.stage.in_([Stage.AWAITING_REP_APPROVAL, Stage.APPROVED]),
        )
        .values(stage=Stage.REJECTED, rep_id=rep_id)
    )
    return result.rowcount == 1
