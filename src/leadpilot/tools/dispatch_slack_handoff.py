"""dispatch_slack_handoff — Step 2 tool (Marc, Group B, Decision 032).

Stages a back-office handoff message (execution-gating rule, PRD
v1.05 3a): this tool only ever calls gate.create_draft, never
gate.try_execute — that authorization is the rep's approval action,
wired in Step 3. No exception for urgent_callback_request (Decision
019) — urgency is not a reason to skip rep approval, even for an
internal-only recipient.

Schema note: architecture/state-schema.md's contact_history table has
no dedicated column for a Slack message's type
(completion_handoff/info_request/urgent_callback_request), even though
the PRD tracks it as an output of this tool. Stored in the `note`
field for now (documented there as "free text", not exclusively for
call outcomes) rather than unilaterally changing shared schema —
flagging this for Abdoul to confirm or replace with a real column.

execute_dispatch_slack_handoff() takes an injectable `slack_client`
(defaults to a real slack_sdk WebClient) specifically so this can be
tested without a live Slack token or network access — same reasoning
as db_session tests running against real Postgres for logic that
matters, but a real external API call isn't something a test should
require to prove the staging/gating/fan-out logic is correct.
"""

import uuid

from sqlalchemy.orm import Session

from leadpilot import gate
from leadpilot.config import settings
from leadpilot.models.contact_history import Channel, ContactHistory, Stage, Tool
from leadpilot.tools.base import tool

_MESSAGE_TYPES = ("completion_handoff", "info_request", "urgent_callback_request")


def _channel_ids() -> list[str]:
    return [c.strip() for c in settings.slack_handoff_channel_ids.split(",") if c.strip()]


@tool(
    name="dispatch_slack_handoff",
    description=(
        "Stages a back-office handoff message to the designated Slack "
        "stakeholders — a standard completion handoff, a request for "
        "additional information, or an urgent callback request. Stages "
        "only; the real Slack message fires only after rep approval, with "
        "no autonomous-send exception for urgent messages."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "lead_id": {
                "type": "string",
                "format": "uuid",
                "description": "The lead this handoff concerns.",
            },
            "message_type": {
                "type": "string",
                "enum": list(_MESSAGE_TYPES),
            },
            "message": {
                "type": "string",
                "description": "The drafted handoff message text to post to Slack.",
            },
        },
        "required": ["lead_id", "message_type", "message"],
    },
)
def dispatch_slack_handoff(
    session: Session, *, lead_id: uuid.UUID | str, message_type: str, message: str
) -> dict:
    """Stages a handoff draft. Raises ValueError for an unrecognized
    message_type rather than silently staging something Slack-side
    code won't know how to handle, and if no stakeholder channels are
    configured yet (SLACK_HANDOFF_CHANNEL_IDS empty in .env.local) —
    staging something with nowhere to send is a config problem worth
    surfacing now, not at approval time.
    """
    if isinstance(lead_id, str):
        lead_id = uuid.UUID(lead_id)
    if message_type not in _MESSAGE_TYPES:
        raise ValueError(f"Unrecognized message_type {message_type!r} — must be one of {_MESSAGE_TYPES}")
    if not message.strip():
        raise ValueError("message cannot be empty")
    if not _channel_ids():
        raise ValueError(
            "SLACK_HANDOFF_CHANNEL_IDS is empty in .env.local — nowhere to send this handoff"
        )

    event = gate.create_draft(
        session,
        lead_id=lead_id,
        channel=Channel.SLACK_HANDOFF,
        tool=Tool.DISPATCH_SLACK_HANDOFF,
        content_ref=message,
    )
    event.note = message_type
    session.flush()

    return {
        "event_id": str(event.event_id),
        "stage": event.stage.value,
        "message_type": message_type,
        "channel_ids": _channel_ids(),
    }


def execute_dispatch_slack_handoff(
    session: Session, *, event_id: uuid.UUID | str, slack_client=None
) -> dict | None:
    """Called by Step 3's approval endpoint, never by the agent. Flips
    the row via gate.try_execute() first — only if that succeeds does
    this post anything — then fans the message out to every configured
    stakeholder channel (PRD: "exactly 3 stakeholder accounts").
    Returns None (posts nothing) if the row wasn't actually approved,
    wasn't a Slack handoff event, or wasn't found.

    slack_client defaults to a real slack_sdk WebClient built from
    settings.slack_bot_token — inject a fake for tests. Deliberately
    imported lazily (inside this function) rather than at module level
    so importing this module doesn't require slack_sdk to be installed
    for callers that only need the staging half.
    """
    if isinstance(event_id, str):
        event_id = uuid.UUID(event_id)

    event = session.get(ContactHistory, event_id)
    if event is None or event.channel != Channel.SLACK_HANDOFF or event.tool != Tool.DISPATCH_SLACK_HANDOFF:
        return None

    if not gate.try_execute(session, event_id):
        return None

    if slack_client is None:
        from slack_sdk import WebClient

        slack_client = WebClient(token=settings.slack_bot_token)

    deliveries = []
    for channel_id in _channel_ids():
        response = slack_client.chat_postMessage(channel=channel_id, text=event.content_ref)
        deliveries.append(
            {
                "channel_id": channel_id,
                "ok": bool(response.get("ok")),
                "ts": response.get("ts"),
            }
        )

    return {
        "event_id": str(event.event_id),
        "message_type": event.note,
        "deliveries": deliveries,
    }
