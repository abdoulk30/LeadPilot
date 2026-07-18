"""On-demand, single-lead email drafting for the rep-facing "Draft with
AI" button on the email compose form (chat request, 2026-07-18).

Deliberately NOT a call into agent_loop.py's batch tool-calling loop:
that loop makes an autonomous decide-whether-and-what-to-do-across-
every-channel judgment for a whole batch run, with its own guards
(rep_id stripped from tool schemas, LeadActionLock, no execute path
exposed). This is a much narrower, synchronous ask a rep triggers
because they already decided they want an email drafted for this one
lead right now — a single Messages API call, no tool use, returning
subject+body text for the rep to review and edit. The rep still has to
submit the compose form themselves to actually stage it (gate.py is
untouched here, same as the blank-form path).

Uses a separate, cheaper/faster model (settings.anthropic_draft_model)
than the batch job's — a rep is waiting live in the UI for this one.
"""

import json
import uuid

from sqlalchemy.orm import Session

from leadpilot.config import settings
from leadpilot.models.leads import Lead
from leadpilot.models.rep import Rep
from leadpilot.tools.get_contact_history import get_contact_history

SYSTEM_PROMPT = (
    "You draft a single outreach email for a sales rep, at their request, based "
    "only on the lead profile and contact history given to you. Never invent "
    "facts (names, prior conversations, commitments) that aren't in that data. "
    "Sign with the sender's name if given; if the sender's company/contact info "
    "isn't given, leave a generic bracketed placeholder for just that piece "
    "(e.g. [Your Company]) rather than inventing one. Keep it professional, "
    "concise, and appropriate to how much contact has already happened — a "
    "first-touch email reads differently from a follow-up. Respond with strict "
    "JSON of the shape {\"subject\": \"...\", \"body\": \"...\"} and nothing else — "
    "no markdown fence, no commentary."
)


class LeadNotFoundError(Exception):
    pass


def _build_context(session: Session, lead_id: uuid.UUID, rep: Rep | None) -> str:
    lead = session.get(Lead, lead_id)
    if lead is None:
        raise LeadNotFoundError(f"No lead found for lead_id={lead_id}")
    if not lead.primary_email:
        raise ValueError(f"Lead {lead_id} has no email address on file — nothing to draft to")

    history = get_contact_history(session, lead_id=lead_id)
    lines = [
        f"You are writing on behalf of: {rep.display_name or rep.email if rep else '(unknown sender — sign generically)'}",
        f"Lead: {lead.display_name or '(no name)'}",
        f"Company: {lead.company or '(unknown)'}",
        f"Phone on file: {lead.primary_phone or '(none)'}",
        f"Email: {lead.primary_email}",
        "",
    ]
    if not history:
        lines.append("Contact history: none yet — this would be the first outreach to this lead.")
    else:
        lines.append("Contact history (most recent first):")
        for event in history[:20]:
            lines.append(
                f"- {event['timestamp']} [{event['channel']}/{event['tool']}] "
                f"stage={event['stage']} outcome={event['outcome']}"
                + (f" note={event['note']}" if event['note'] else "")
            )
    return "\n".join(lines)


def _parse_draft(text: str) -> dict:
    """Same tolerant-JSON-extraction pattern as agent_loop.py's
    _parse_report — strips a markdown fence if present, falls back to
    locating the first {...} object in mixed text.
    """
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("\n", 1)[1] if "\n" in cleaned else cleaned
        cleaned = cleaned.rsplit("```", 1)[0]
    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError:
        start = cleaned.find("{")
        if start == -1:
            raise
        parsed, _ = json.JSONDecoder().raw_decode(cleaned[start:])
    if "subject" not in parsed or "body" not in parsed:
        raise ValueError(f"Model did not return both subject and body: {parsed!r}")
    return {"subject": parsed["subject"], "body": parsed["body"]}


def draft_email_for_lead(
    session: Session, lead_id: uuid.UUID, rep: Rep | None = None, anthropic_client=None
) -> dict:
    """Returns {"subject": str, "body": str}. Raises LeadNotFoundError /
    ValueError for the same missing-lead/no-email reasons
    send_lead_email raises — checked before spending an API call.

    `rep` is the approving rep, purely so the draft can sign with their
    real name instead of a placeholder — optional since callers that
    don't have it handy (or tests) still get a usable, generically
    signed draft.
    """
    context = _build_context(session, lead_id, rep)

    if anthropic_client is None:
        import anthropic
        anthropic_client = anthropic.Anthropic(api_key=settings.anthropic_api_key or None)

    response = anthropic_client.messages.create(
        model=settings.anthropic_draft_model,
        max_tokens=800,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": context}],
    )
    text = "".join(block.text for block in response.content if block.type == "text")
    return _parse_draft(text)
