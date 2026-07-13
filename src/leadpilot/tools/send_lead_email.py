"""send_lead_email — Step 2 tool (Marc, Group B, Decision 032).

Stages a drafted email (execution-gating rule, PRD v1.05 3a): this
tool only ever calls gate.create_draft, never gate.try_execute — that
authorization is the rep's approval action, wired in Step 3.

Sent from the *approving rep's own Gmail account*, per-rep OAuth
(Decision 026, extended 2026-07-13 to add the gmail.send scope —
google_oauth.py's SCOPES list) — not a shared service account, same
reasoning as GoogleSheetsConnector. The rep who approves is whoever
gate.approve() records on the row (event.rep_id), so
execute_send_lead_email() mints that specific rep's access token, not
a static configured sender.

content_ref stores the drafted subject+body as JSON rather than
splitting across content_ref/note the way dispatch_slack_handoff had
to — content_ref is documented as "the drafted content," and an
email's content naturally includes both parts, so this doesn't
overload a field for something outside its documented meaning the way
`note` would (see decisions/README.md's open item on that).
"""

import base64
import json
import uuid
from email.mime.text import MIMEText

from google.oauth2.credentials import Credentials as GoogleCredentials
from googleapiclient.discovery import build
from sqlalchemy.orm import Session

from leadpilot import gate, google_oauth
from leadpilot.connectors.google_sheets import RepNotConnectedError
from leadpilot.models.contact_history import Channel, ContactHistory, Stage, Tool
from leadpilot.models.leads import Lead
from leadpilot.tools.base import tool


@tool(
    name="send_lead_email",
    description=(
        "Drafts an email to a lead. Stages only; the real email is sent, "
        "from the approving rep's own Gmail account, only after rep "
        "approval."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "lead_id": {
                "type": "string",
                "format": "uuid",
                "description": "The lead to email.",
            },
            "subject": {"type": "string"},
            "body": {"type": "string"},
        },
        "required": ["lead_id", "subject", "body"],
    },
)
def send_lead_email(session: Session, *, lead_id: uuid.UUID | str, subject: str, body: str) -> dict:
    """Looks up the lead's own email address (not a caller-supplied one
    — same reasoning as initiate_lead_call's phone-number lookup) and
    stages a draft. Raises ValueError if the lead has no email on
    file, or subject/body is empty.
    """
    if isinstance(lead_id, str):
        lead_id = uuid.UUID(lead_id)
    if not subject.strip():
        raise ValueError("subject cannot be empty")
    if not body.strip():
        raise ValueError("body cannot be empty")

    lead = session.get(Lead, lead_id)
    if lead is None:
        raise ValueError(f"No lead found for lead_id={lead_id}")
    if not lead.primary_email:
        raise ValueError(f"Lead {lead_id} has no email address on file — nothing to send to")

    event = gate.create_draft(
        session,
        lead_id=lead_id,
        channel=Channel.EMAIL,
        tool=Tool.SEND_LEAD_EMAIL,
        content_ref=json.dumps({"subject": subject, "body": body, "to": lead.primary_email}),
    )

    return {
        "event_id": str(event.event_id),
        "stage": event.stage.value,
        "to": lead.primary_email,
        "subject": subject,
    }


def execute_send_lead_email(session: Session, *, event_id: uuid.UUID | str, gmail_service=None) -> dict | None:
    """Called by Step 3's approval endpoint, never by the agent. Sends
    via the *approving* rep's own Gmail account (event.rep_id, set by
    gate.approve()), matching per-rep OAuth (Decision 026) — never a
    shared/static sender.

    Deliberately builds/validates gmail_service *before* calling
    gate.try_execute() — try_execute() is a one-way flip (Decision
    021), so checking the rep's Google connection only after claiming
    execution rights would leave a row falsely marked EXECUTED for an
    email that was never actually sent, if the rep turned out not to
    be connected. Only claim execution once we're actually able to
    attempt the send. (This doesn't cover every failure mode — a
    Gmail API error during the send call itself, after try_execute()
    succeeds, can still leave a row marked EXECUTED with nothing
    delivered. That's an existing gap in gate.py's flip-then-act
    pattern generally, not something specific to this tool or safe
    for one tool to unilaterally redesign — worth raising with Abdoul
    alongside the other open schema items.)

    gmail_service defaults to a real googleapiclient Gmail resource
    built from that rep's fresh access token — inject a fake for
    tests, same reasoning as dispatch_slack_handoff's slack_client.
    Raises RepNotConnectedError (shared with GoogleSheetsConnector) if
    the approving rep hasn't connected a Google account or has since
    revoked it — a real, user-facing error, not silently skipped.
    """
    if isinstance(event_id, str):
        event_id = uuid.UUID(event_id)

    event = session.get(ContactHistory, event_id)
    if event is None or event.channel != Channel.EMAIL or event.tool != Tool.SEND_LEAD_EMAIL:
        return None
    if event.stage != Stage.APPROVED:
        return None

    if gmail_service is None:
        access_token = google_oauth.get_fresh_access_token(session, event.rep_id)
        if access_token is None:
            raise RepNotConnectedError(
                f"Rep {event.rep_id} has not connected a Google account (or has since revoked it) "
                "— cannot send from their Gmail"
            )
        creds = GoogleCredentials(token=access_token)
        gmail_service = build("gmail", "v1", credentials=creds)

    if not gate.try_execute(session, event_id):
        return None

    draft = json.loads(event.content_ref)

    message = MIMEText(draft["body"])
    message["to"] = draft["to"]
    message["subject"] = draft["subject"]
    raw = base64.urlsafe_b64encode(message.as_bytes()).decode()

    response = gmail_service.users().messages().send(userId="me", body={"raw": raw}).execute()

    return {
        "event_id": str(event.event_id),
        "to": draft["to"],
        "message_id": response.get("id"),
    }
