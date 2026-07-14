"""get_contact_history — Step 2 tool (Marc, Group B, Decision 032).

Read-only, no approval gate needed — PRD v1.05 3a lists this alongside
fetch_all_leads/verify_drive_contents/fetch_ad_hoc_sheet as tools with
no real-world side effect, so nothing here ever calls gate.create_draft
or gate.try_execute. Reads LeadPilot's own append-only contact-history
log (architecture/state-schema.md) instead of querying any external
call-log service — Decision 018, closing Issue 001 (Google Voice has
no API to poll).

Used by the system prompt's prioritization step ("Cross-reference
every active lead by calling get_contact_history", PRD v1.05 3b step
2) to determine Rank 1/2/3 and whether a call's rep-reported outcome
is known yet. Also the natural read path for Step 3's queue view and
communications search box — kept as a plain importable function (not
just a registry entry) so dashboard code can call it directly, the
same way gate.py's functions are called directly outside the tool
registry.
"""

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from leadpilot.models.contact_history import ContactHistory
from leadpilot.tools.base import tool


@tool(
    name="get_contact_history",
    description=(
        "Reads LeadPilot's own append-only contact-history log for a single "
        "lead, instead of querying any external call-log service. Returns "
        "every contact-related event for that lead (call/text/email/Slack "
        "handoff/sheet edit), most recent first, including each event's "
        "stage, rep-reported outcome where available, message_type for Slack "
        "handoffs, and who approved it. "
        "Call outcomes are null/'pending' until the rep reports back via "
        "log_call_outcome — that is expected, not missing data."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "lead_id": {
                "type": "string",
                "format": "uuid",
                "description": "The canonical lead_id (post-dedup) to fetch contact history for.",
            }
        },
        "required": ["lead_id"],
    },
)
def get_contact_history(session: Session, *, lead_id: uuid.UUID | str) -> list[dict]:
    """Returns every contact_history row for `lead_id`, most recent
    first. Accepts either a uuid.UUID (direct Python callers, e.g. the
    dashboard) or a str (the eventual SDK tool-calling loop, which
    passes JSON-schema-validated string input) — normalized to UUID
    before querying either way.

    Returns a plain list of dicts rather than ORM objects, since this
    is a read-only boundary both the tool-calling loop and Step 3's
    interface consume — neither should need to import
    leadpilot.models.contact_history just to read a lead's history.
    """
    if isinstance(lead_id, str):
        lead_id = uuid.UUID(lead_id)

    rows = (
        session.execute(
            select(ContactHistory)
            .where(ContactHistory.lead_id == lead_id)
            .order_by(ContactHistory.timestamp.desc())
        )
        .scalars()
        .all()
    )

    return [
        {
            "event_id": str(row.event_id),
            "channel": row.channel.value,
            "tool": row.tool.value,
            "stage": row.stage.value,
            "timestamp": row.timestamp.isoformat(),
            "rep_id": str(row.rep_id) if row.rep_id is not None else None,
            "outcome": row.outcome.value if row.outcome is not None else None,
            "content_ref": row.content_ref,
            "note": row.note,
            "message_type": row.message_type.value if row.message_type is not None else None,
        }
        for row in rows
    ]
