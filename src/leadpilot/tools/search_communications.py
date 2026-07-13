"""search_communications — Step 2 tool (Marc, Group B, Decision 032).

Read-only (PRD v1.05 3a lists this alongside get_contact_history/
fetch_all_leads as needing no approval gate) — searches a lead's email
and text history by any known identifier (email, phone, full name, or
company name).

Two real API limitations, documented here rather than silently
papered over:

1. Email search (Gmail, per-rep OAuth — same gmail.readonly scope
   added to google_oauth.py alongside send_lead_email's gmail.send)
   works for any identifier, since Gmail's `q=` query param free-text
   searches across from/to/subject/body. Only the first page of
   results is fetched — no pagination handling yet, since neither the
   PRD nor an eval case specifies a page-size/pagination requirement.

2. SMS search (Twilio) only runs for phone-number-shaped identifiers.
   Twilio's Messages resource filters by exact From/To phone number,
   not free text — there's no way to search SMS body content for a
   name or company via the basic Messages API. A name/company search
   only returns email results; texts stays empty for those. This is a
   real, load-bearing gap in what this tool can do for name/company
   lookups against SMS specifically, not an oversight to quietly work
   around later.

**Live-verification status:** same caveat as send_lead_text (Issue
005) for the Twilio half — untested against the real API. The Gmail
half is untested against a real connected account until someone
completes the live OAuth flow (same gap send_lead_email has).
"""

import re
import uuid

from google.oauth2.credentials import Credentials as GoogleCredentials
from googleapiclient.discovery import build
from sqlalchemy.orm import Session

from leadpilot import google_oauth
from leadpilot.config import settings
from leadpilot.connectors.google_sheets import RepNotConnectedError
from leadpilot.tools.base import tool

_PHONE_CHARS = re.compile(r"[\s\-()+ ]")


def _looks_like_phone(identifier: str) -> bool:
    stripped = _PHONE_CHARS.sub("", identifier)
    return stripped.isdigit() and len(stripped) >= 7


@tool(
    name="search_communications",
    description=(
        "Searches a lead's email and text message history using any known "
        "identifier — email address, phone number, full name, or company "
        "name. Phone-number identifiers search both email and SMS; "
        "name/company identifiers only search email, since SMS has no "
        "free-text body search available. Read-only, no approval gate."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "rep_id": {
                "type": "string",
                "format": "uuid",
                "description": "Which rep's connected Gmail account to search (per-rep OAuth).",
            },
            "identifier": {
                "type": "string",
                "description": "Email address, phone number, full name, or company name to search for.",
            },
        },
        "required": ["rep_id", "identifier"],
    },
)
def search_communications(
    session: Session,
    *,
    rep_id: uuid.UUID | str,
    identifier: str,
    gmail_service=None,
    twilio_client=None,
) -> dict:
    if isinstance(rep_id, str):
        rep_id = uuid.UUID(rep_id)
    if not identifier.strip():
        raise ValueError("identifier cannot be empty")

    results: dict = {"identifier": identifier, "emails": [], "texts": []}

    if gmail_service is None:
        access_token = google_oauth.get_fresh_access_token(session, rep_id)
        if access_token is None:
            raise RepNotConnectedError(
                f"Rep {rep_id} has not connected a Google account (or has since revoked it) "
                "— cannot search their Gmail"
            )
        creds = GoogleCredentials(token=access_token)
        gmail_service = build("gmail", "v1", credentials=creds)

    list_response = gmail_service.users().messages().list(userId="me", q=identifier).execute()
    for stub in list_response.get("messages", []):
        detail = (
            gmail_service.users()
            .messages()
            .get(
                userId="me",
                id=stub["id"],
                format="metadata",
                metadataHeaders=["From", "To", "Subject", "Date"],
            )
            .execute()
        )
        headers = {h["name"]: h["value"] for h in detail.get("payload", {}).get("headers", [])}
        parts = detail.get("payload", {}).get("parts") or []
        has_attachment = any(part.get("filename") for part in parts)
        results["emails"].append(
            {
                "message_id": detail.get("id"),
                "from": headers.get("From"),
                "to": headers.get("To"),
                "subject": headers.get("Subject"),
                "date": headers.get("Date"),
                "snippet": detail.get("snippet"),
                "has_attachment": has_attachment,
            }
        )

    if _looks_like_phone(identifier):
        if twilio_client is None:
            from twilio.rest import Client

            twilio_client = Client(settings.twilio_account_sid, settings.twilio_auth_token)

        seen_sids: set[str] = set()
        for kwargs in ({"from_": identifier}, {"to": identifier}):
            for msg in twilio_client.messages.list(**kwargs):
                if msg.sid in seen_sids:
                    continue
                seen_sids.add(msg.sid)
                results["texts"].append(
                    {
                        "message_sid": msg.sid,
                        "from": msg.from_,
                        "to": msg.to,
                        "body": msg.body,
                        "date_sent": str(msg.date_sent) if msg.date_sent else None,
                        "direction": msg.direction,
                    }
                )

    return results
