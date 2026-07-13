"""send_lead_text — Step 2 tool (Marc, Group B, Decision 032).

Stages a drafted text message (execution-gating rule, PRD v1.05 3a):
this tool only ever calls gate.create_draft, never gate.try_execute —
that authorization is the rep's approval action, wired in Step 3.

Sent via Twilio (Decision 022), from the single configured
TWILIO_FROM_NUMBER — unlike Gmail, Twilio isn't per-rep OAuth, it's one
account-level credential (TWILIO_ACCOUNT_SID/AUTH_TOKEN), same as every
other tool that uses it.

**Live-verification status (testing/known-issues-log.md Issue 005):**
the Twilio trial account's credentials authenticate fine (200 on the
base Account resource), but two *other* endpoints
(IncomingPhoneNumbers, OutgoingCallerIds) return 401 Policy evaluation
failed for reasons not yet diagnosed. This tool uses neither of those
endpoints — messages.create() is a different endpoint entirely — so
it may well work even while Issue 005 is unresolved. Not confirmed
either way; treat this tool as built and unit-tested against a fake
client, not verified against the real API, until someone actually
runs it with real credentials.
"""

import uuid

from sqlalchemy.orm import Session

from leadpilot import gate
from leadpilot.config import settings
from leadpilot.models.contact_history import Channel, ContactHistory, Stage, Tool
from leadpilot.models.leads import Lead
from leadpilot.tools.base import tool


@tool(
    name="send_lead_text",
    description=(
        "Drafts a text message to a lead (e.g. a document-request nudge or "
        "cadence follow-up). Stages only; the real SMS is sent only after "
        "rep approval."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "lead_id": {
                "type": "string",
                "format": "uuid",
                "description": "The lead to text.",
            },
            "message": {"type": "string"},
        },
        "required": ["lead_id", "message"],
    },
)
def send_lead_text(session: Session, *, lead_id: uuid.UUID | str, message: str) -> dict:
    """Looks up the lead's own phone number (not a caller-supplied one
    — same reasoning as initiate_lead_call/send_lead_email). Raises
    ValueError if the lead has no phone number on file or message is
    empty.
    """
    if isinstance(lead_id, str):
        lead_id = uuid.UUID(lead_id)
    if not message.strip():
        raise ValueError("message cannot be empty")

    lead = session.get(Lead, lead_id)
    if lead is None:
        raise ValueError(f"No lead found for lead_id={lead_id}")
    if not lead.primary_phone:
        raise ValueError(f"Lead {lead_id} has no phone number on file — nothing to text")

    event = gate.create_draft(
        session,
        lead_id=lead_id,
        channel=Channel.TEXT,
        tool=Tool.SEND_LEAD_TEXT,
        content_ref=message,
    )

    return {
        "event_id": str(event.event_id),
        "stage": event.stage.value,
        "to": lead.primary_phone,
        "message": message,
    }


def execute_send_lead_text(session: Session, *, event_id: uuid.UUID | str, twilio_client=None) -> dict | None:
    """Called by Step 3's approval endpoint, never by the agent.
    Builds/validates the Twilio client *before* calling
    gate.try_execute() — same reasoning as send_lead_email's ordering
    fix: try_execute() is a one-way flip, so a missing/misconfigured
    TWILIO_FROM_NUMBER shouldn't leave a row falsely marked EXECUTED
    for a text that was never sent.

    twilio_client defaults to a real twilio.rest.Client built from
    settings — inject a fake for tests. Raises ValueError if
    TWILIO_FROM_NUMBER isn't configured, same "fail loudly on a config
    gap" approach as dispatch_slack_handoff's channel-ids check.
    """
    if isinstance(event_id, str):
        event_id = uuid.UUID(event_id)

    event = session.get(ContactHistory, event_id)
    if event is None or event.channel != Channel.TEXT or event.tool != Tool.SEND_LEAD_TEXT:
        return None
    if event.stage != Stage.APPROVED:
        return None

    if not settings.twilio_from_number:
        raise ValueError("TWILIO_FROM_NUMBER is empty in .env.local — nothing to send from")

    if twilio_client is None:
        from twilio.rest import Client

        twilio_client = Client(settings.twilio_account_sid, settings.twilio_auth_token)

    if not gate.try_execute(session, event_id):
        return None

    lead = session.get(Lead, event.lead_id)

    message = twilio_client.messages.create(
        body=event.content_ref,
        from_=settings.twilio_from_number,
        to=lead.primary_phone,
    )

    return {
        "event_id": str(event.event_id),
        "to": lead.primary_phone,
        "message_sid": message.sid,
        "status": message.status,
    }
