"""update_lead_sheet — PRD v1.05 §3a. Writes a rep-approved edit (status
update, note, dedup merge) back to the source Google Sheet, authenticated
as the *approving* rep — so Google's own revision history attributes the
edit to that specific rep, matching the existing contact_history
attribution (Decision 013).

Split into two functions, not one, because the real write can only
legally happen after a separate rep-approval step (Decision 021) that
this module doesn't own:

  - run(): the @tool the agent calls. Computes the diff via
    connector.stage_field_write() and drops a contact_history row at
    AWAITING_REP_APPROVAL. Never writes.
  - execute(): called by whatever handles the rep's "Approve" click
    (gate.approve(), then this — Step 3/interface territory, doesn't
    exist yet). Re-checks gate.try_execute() itself rather than trusting
    the caller already did, then performs the real write via
    connector.commit_field_write(), authenticated as event.rep_id (set
    by gate.approve(), not by run()).

All the information execute() needs to know *what* to write is encoded
into content_ref as JSON at stage time — ContactHistory has nowhere else
to put it, and event_id is the only handle execute() is given. That now
includes `current` (Decision 034) — the FieldDiff.current value the rep
actually saw and reviewed at staging time — not just what to write, so
execute() can pass it to commit_field_write as `expected_current` and
let the connector detect a stale/superseded approval rather than
blindly overwriting.

Commit boundary around try_execute() mirrors fetch_all_leads' run-lock:
committed immediately so the row lock isn't held for the duration of the
external Sheets API call. That means a failure in the Sheets call itself
happens *after* the event is already marked EXECUTED — see
WriteExecutionFailedAfterApprovalError. Trades perfect DB/reality
consistency for not holding a Postgres row lock open across a network
call; the alternative (roll the stage flip back on write failure) would
let a slow rep's retry race a second try_execute() while the first
attempt's outcome is still unknown.

StaleWriteError/ConcurrentWriteError (connectors/base.py, Decision 034)
are deliberately NOT wrapped into WriteExecutionFailedAfterApprovalError
— they propagate as-is. Per that module's docstring, a future Step 3
caller needs to treat those two specifically as "show the rep a fresh
diff and ask them to re-approve," not as an opaque failure worth
generic error handling; collapsing them into
WriteExecutionFailedAfterApprovalError would destroy that distinction.
"""

import json
import uuid

from sqlalchemy.orm import Session

from leadpilot import gate
from leadpilot.connectors.base import ConcurrentWriteError, LeadSourceConnector, StaleWriteError
from leadpilot.connectors.google_sheets import GoogleSheetsConnector
from leadpilot.models.contact_history import Channel, ContactHistory, Tool
from leadpilot.tools.base import tool


class WriteExecutionFailedAfterApprovalError(Exception):
    """The contact_history row was already flipped to EXECUTED (this
    caller legitimately won try_execute()) but the real Google Sheets
    write itself raised — for a reason *other* than a stale/conflicting
    value (see StaleWriteError/ConcurrentWriteError, which propagate
    separately, not through this). The approval was consumed — a retry
    needs a fresh run()/approve() cycle, not another call to this
    event_id.
    """


def _encode_content_ref(source_id: str, row_ref: str, field: str, current: str | None, value: str) -> str:
    return json.dumps(
        {"source_id": source_id, "row_ref": row_ref, "field": field, "current": current, "value": value}
    )


def _decode_content_ref(content_ref: str) -> dict:
    return json.loads(content_ref)


@tool(
    name="update_lead_sheet",
    description=(
        "Stages a rep-approved edit to a lead's source Google Sheet — computes a current-vs-"
        "proposed diff for a single field and creates an awaiting-approval record. Does not write "
        "anything yet; the real write only happens after the rep approves via the approval gate."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "rep_id": {"type": "string", "description": "The requesting rep's UUID"},
            "lead_id": {"type": "string", "description": "The canonical lead UUID this edit is for"},
            "source_id": {"type": "string", "description": "The Google Sheet file ID containing the row"},
            "row_ref": {"type": "string", "description": "The row identifier within that sheet"},
            "field": {"type": "string", "description": "Abstracted field name, e.g. 'status'"},
            "value": {"type": "string", "description": "The proposed new value"},
        },
        "required": ["rep_id", "lead_id", "source_id", "row_ref", "field", "value"],
    },
)
def run(
    session: Session,
    rep_id: uuid.UUID,
    lead_id: uuid.UUID,
    source_id: str,
    row_ref: str,
    field: str,
    value: str,
    connector: LeadSourceConnector | None = None,
) -> dict:
    connector = connector or GoogleSheetsConnector(session, rep_id)
    try:
        diff = connector.stage_field_write(source_id, row_ref, field, value)
        event = gate.create_draft(
            session,
            lead_id=lead_id,
            channel=Channel.SHEET_EDIT,
            tool=Tool.UPDATE_LEAD_SHEET,
            content_ref=_encode_content_ref(source_id, row_ref, field, diff.current, value),
        )
        session.commit()
        return {
            "event_id": str(event.event_id),
            "status": "awaiting_rep_approval",
            "field": diff.field,
            "current": diff.current,
            "proposed": diff.proposed,
        }
    except Exception:
        session.rollback()
        raise


def execute(session: Session, event_id: uuid.UUID, connector: LeadSourceConnector | None = None) -> dict:
    event = session.get(ContactHistory, event_id)
    if event is None:
        raise ValueError(f"No such contact_history event: {event_id}")
    if event.tool != Tool.UPDATE_LEAD_SHEET:
        raise ValueError(f"Event {event_id} is not an update_lead_sheet draft (tool={event.tool})")

    info = _decode_content_ref(event.content_ref)
    rep_id = event.rep_id  # set by gate.approve(); the write is attributed to this rep

    won = gate.try_execute(session, event_id)
    session.commit()
    if not won:
        return {"executed": False}

    connector = connector or GoogleSheetsConnector(session, rep_id)
    try:
        connector.commit_field_write(
            info["source_id"],
            info["row_ref"],
            info["field"],
            info["value"],
            expected_current=info["current"],
        )
    except (StaleWriteError, ConcurrentWriteError):
        raise
    except Exception as e:
        raise WriteExecutionFailedAfterApprovalError(
            f"contact_history event {event_id} is marked executed, but the real Sheets write failed: {e}"
        ) from e

    return {
        "executed": True,
        "source_id": info["source_id"],
        "row_ref": info["row_ref"],
        "field": info["field"],
        "value": info["value"],
    }
