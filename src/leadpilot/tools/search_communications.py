"""search_communications — Step 2 tool (Marc, Group B, Decision 032).

Read-only (PRD v1.05 3a lists this alongside get_contact_history/
fetch_all_leads as needing no approval gate) — searches a lead's email
and text history by any known identifier (email, phone, full name, or
company name).

Lead-aware identifier expansion (testing/eval-suite.md Case 4, fixed
2026-07-14): a rep searching by *one* identifier (e.g. a phone number)
must still surface communications tied to that same lead's *other*
known identifiers (email, name, company) — a message that only
mentions the lead's email wouldn't otherwise turn up. Resolves the
provided identifier to a canonical Lead the same way the UI's search
route already does for its identity header (exact phone, exact email,
substring match on name/company), then searches Gmail using an OR
query across every non-null identifier that lead actually has — not
just the one the rep typed. Falls back to a literal search on the raw
identifier if it doesn't resolve to any known lead (e.g. an external
number/address not yet in the system), preserving the original
behavior for that case. Twilio search targets the resolved lead's own
phone number when one exists, rather than only the rep-typed string —
same "look up the lead's own field, don't trust a caller-supplied
value" pattern every other tool in this codebase already follows.

Two real API limitations, documented here rather than silently
papered over:

1. Email search (Gmail, per-rep OAuth — same gmail.readonly scope
   added to google_oauth.py alongside send_lead_email's gmail.send)
   works for any identifier, since Gmail's `q=` query param free-text
   searches across from/to/subject/body. Only the first page of
   results is fetched — no pagination handling yet, since neither the
   PRD nor an eval case specifies a page-size/pagination requirement.

2. SMS search (Twilio) only runs against a real phone number — either
   the rep typed one, or the identifier resolved to a known Lead that
   has one on file (Case 4: searching "Acme Corp" should still surface
   that lead's texts, found via *their* phone number, not the company
   name). Twilio's Messages resource filters by exact From/To phone
   number, not free text, so there's genuinely no way to search SMS
   body content by name/company directly — the gap that remains is
   narrower than it used to be: a name/company identifier with no
   resolvable Lead (nothing in the system yet) still returns no texts,
   since there's no phone number to search with at all. Not an
   oversight to quietly work around later.

**Live-verification status:** same caveat as send_lead_text (Issue
005) for the Twilio half — untested against the real API. The Gmail
half is untested against a real connected account until someone
completes the live OAuth flow (same gap send_lead_email has).
"""

import re
import uuid

from google.oauth2.credentials import Credentials as GoogleCredentials
from googleapiclient.discovery import build
from sqlalchemy import select
from sqlalchemy.orm import Session

from leadpilot import google_oauth
from leadpilot.config import settings
from leadpilot.connectors.google_sheets import RepNotConnectedError
from leadpilot.models.leads import Lead
from leadpilot.tools.base import tool

_PHONE_CHARS = re.compile(r"[\s\-()+ ]")


def _looks_like_phone(identifier: str) -> bool:
    stripped = _PHONE_CHARS.sub("", identifier)
    return stripped.isdigit() and len(stripped) >= 7


def _find_lead_by_identifier(session: Session, identifier: str) -> Lead | None:
    """Same resolution ui.py's search route already uses for its lead-
    identity header: exact phone, exact (lowercased) email, or a
    substring match on name/company. Kept here too (not just in ui.py)
    since this tool's own documented contract — searching "across all
    of a lead's known identifiers" — depends on it; a caller shouldn't
    have to duplicate this resolution correctly for the tool to work.
    """
    stmt = select(Lead).where(
        (Lead.primary_phone == identifier)
        | (Lead.primary_email == identifier.lower())
        | (Lead.display_name.ilike(f"%{identifier}%"))
        | (Lead.company.ilike(f"%{identifier}%"))
    )
    return session.execute(stmt).scalars().first()


def _gmail_query_for(lead: Lead | None, fallback_identifier: str) -> str:
    if lead is None:
        return fallback_identifier
    terms = []
    if lead.primary_email:
        terms.append(lead.primary_email)
    if lead.primary_phone:
        terms.append(lead.primary_phone)
    if lead.display_name:
        terms.append(f'"{lead.display_name}"')
    if lead.company:
        terms.append(f'"{lead.company}"')
    return " OR ".join(terms) if terms else fallback_identifier


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

    lead = _find_lead_by_identifier(session, identifier)

    if gmail_service is None:
        access_token = google_oauth.get_fresh_access_token(session, rep_id)
        if access_token is None:
            raise RepNotConnectedError(
                f"Rep {rep_id} has not connected a Google account (or has since revoked it) "
                "— cannot search their Gmail"
            )
        creds = GoogleCredentials(token=access_token)
        gmail_service = build("gmail", "v1", credentials=creds)

    gmail_query = _gmail_query_for(lead, identifier)
    list_response = gmail_service.users().messages().list(userId="me", q=gmail_query).execute()
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

    # Search using the resolved lead's own phone number when there is
    # one — same "trust the lead record, not the caller-supplied
    # value" reasoning as _gmail_query_for. Falls back to the raw
    # identifier only when it isn't tied to any known lead.
    phone_to_search = (lead.primary_phone if lead and lead.primary_phone else None) or (
        identifier if _looks_like_phone(identifier) else None
    )
    if phone_to_search:
        if twilio_client is None:
            from twilio.rest import Client

            twilio_client = Client(settings.twilio_account_sid, settings.twilio_auth_token)

        seen_sids: set[str] = set()
        for kwargs in ({"from_": phone_to_search}, {"to": phone_to_search}):
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
