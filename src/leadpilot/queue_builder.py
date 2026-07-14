"""Server-side assembly of the prioritized queue for the Step 3
workspace (design spec v001 §4, interface context v002 "A rep's day").

Interim ranking heuristic — replaced by the real agent output in Step 4:
the PRD's ranks (1: active interest in last 24h / 2: new uncontacted /
3: stale, needs cadence follow-up) are ultimately the agent's judgment
call, but the agent loop doesn't exist yet. Until it does, rank is
computed from what contact_history actually stores:

  - Rank 1: an ANSWERED call within the last 24 hours — the only
    "active interest" signal LeadPilot itself records today.
  - Rank 2: no executed contact events at all (new/uncontacted).
  - Rank 3: contacted before, nothing answered in the last 24h —
    the multi-channel cadence-follow-up bucket.

Each lead carries a human-readable rank_reason so the rep can see WHY
it's ranked where it is (v002: "reps need to trust the ranking, not
just obey it" — >90% adherence is a named success metric). When Step 4
lands, this module keeps the same output shape and swaps the heuristic
for the agent's own queue.

Sorting (spec §6d): leads with a pending urgent_callback_request
handoff sort to the top — urgency changes prominence and sort order,
never the approval gate.
"""

import json
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from leadpilot.injection_guard import FLAGGED_PLACEHOLDER
from leadpilot.models.contact_history import (
    Channel,
    ContactHistory,
    MessageType,
    Outcome,
    Stage,
    Tool,
)
from leadpilot.models.dedup import LeadSourceRow
from leadpilot.models.leads import Lead
from leadpilot.models.rep import Rep

PENDING_STAGES = (Stage.AWAITING_REP_APPROVAL, Stage.APPROVED)

_CHANNEL_LABELS = {
    Channel.CALL: "call",
    Channel.TEXT: "text",
    Channel.EMAIL: "email",
    Channel.SLACK_HANDOFF: "Slack handoff",
    Channel.SHEET_EDIT: "sheet edit",
}


def ago(ts: datetime, now: datetime | None = None) -> str:
    now = now or datetime.now(timezone.utc)
    delta = now - ts
    seconds = max(0, int(delta.total_seconds()))
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        return f"{seconds // 60}m ago"
    if seconds < 86400:
        return f"{seconds // 3600}h ago"
    return f"{seconds // 86400}d ago"


def _rank(events: list[ContactHistory], now: datetime) -> tuple[int, str]:
    executed = [e for e in events if e.stage == Stage.EXECUTED]
    if not executed:
        return 2, "new lead — no contact yet"

    cutoff = now - timedelta(hours=24)
    answered_recent = [
        e for e in executed
        if e.outcome == Outcome.ANSWERED and e.timestamp >= cutoff
    ]
    if answered_recent:
        return 1, f"answered call {ago(answered_recent[0].timestamp, now)} — active interest"

    last = max(executed, key=lambda e: e.timestamp)
    pending_call = any(
        e.tool == Tool.INITIATE_LEAD_CALL and e.outcome == Outcome.PENDING for e in executed
    )
    if pending_call:
        return 3, f"last contact {ago(last.timestamp, now)} — call outcome not logged yet"
    unanswered = any(e.outcome in (Outcome.NO_ANSWER, Outcome.VOICEMAIL) for e in executed)
    if unanswered:
        return 3, f"last contact {ago(last.timestamp, now)} — unanswered, needs multi-channel follow-up"
    return 3, f"last contact {ago(last.timestamp, now)} — due a cadence follow-up"


def _lead_is_flagged(lead: Lead) -> bool:
    return FLAGGED_PLACEHOLDER in (
        lead.display_name, lead.primary_phone, lead.primary_email, lead.company,
    )


def build_queue(session: Session, rep_id: uuid.UUID) -> list[dict]:
    """One summary dict per lead, sorted: urgent-handoff leads first,
    then rank, then most recent activity.
    """
    now = datetime.now(timezone.utc)
    leads = session.execute(select(Lead)).scalars().all()
    all_events = session.execute(select(ContactHistory)).scalars().all()
    by_lead: dict[uuid.UUID, list[ContactHistory]] = {}
    for event in all_events:
        by_lead.setdefault(event.lead_id, []).append(event)

    items = []
    for lead in leads:
        events = by_lead.get(lead.lead_id, [])
        rank, reason = _rank(events, now)
        pending = [e for e in events if e.stage in PENDING_STAGES]
        urgent = any(
            e.message_type == MessageType.URGENT_CALLBACK_REQUEST and e.stage in PENDING_STAGES
            for e in events
        )
        latest = max((e.timestamp for e in events), default=lead.created_at)

        if pending:
            line = f"{len(pending)} pending approval{'s' if len(pending) != 1 else ''}"
        else:
            line = reason

        items.append(
            {
                "lead_id": str(lead.lead_id),
                "name": lead.display_name or "(no name)",
                "company": lead.company,
                "rank": rank,
                "rank_reason": reason,
                "line": line,
                "pending_count": len(pending),
                "urgent": urgent,
                "flagged": _lead_is_flagged(lead),
                "latest": latest,
            }
        )

    items.sort(key=lambda i: (not i["urgent"], i["rank"], -(i["latest"].timestamp() if i["latest"] else 0)))
    return items


def pending_actions(session: Session, lead_id: uuid.UUID) -> list[ContactHistory]:
    """Staged drafts for the center pane — awaiting approval (or
    approved-but-unexecuted, which the normal approve-and-execute flow
    never leaves behind but reject still needs to reach). Urgent
    handoffs sort first (§6d), then oldest staged first.
    """
    rows = (
        session.execute(
            select(ContactHistory)
            .where(ContactHistory.lead_id == lead_id, ContactHistory.stage.in_(PENDING_STAGES))
            .order_by(ContactHistory.timestamp.asc())
        )
        .scalars()
        .all()
    )
    rows.sort(key=lambda r: r.message_type != MessageType.URGENT_CALLBACK_REQUEST)
    return rows


def unlogged_calls(session: Session, rep_id: uuid.UUID) -> list[dict]:
    """This rep's executed calls with no outcome yet — the amber strip
    (§6f moment 2). Scoped to the current rep: an unlogged call is the
    approving rep's own loose end, not a shared to-do.
    """
    rows = (
        session.execute(
            select(ContactHistory, Lead)
            .join(Lead, Lead.lead_id == ContactHistory.lead_id)
            .where(
                ContactHistory.tool == Tool.INITIATE_LEAD_CALL,
                ContactHistory.stage == Stage.EXECUTED,
                ContactHistory.outcome == Outcome.PENDING,
                ContactHistory.rep_id == rep_id,
            )
            .order_by(ContactHistory.timestamp.asc())
        )
        .all()
    )
    return [
        {
            "event_id": str(event.event_id),
            "lead_id": str(lead.lead_id),
            "lead_name": lead.display_name or "(no name)",
            "phone": event.content_ref,
            "when": ago(event.timestamp),
        }
        for event, lead in rows
    ]


def lead_sources(session: Session, lead_id: uuid.UUID) -> list[LeadSourceRow]:
    return (
        session.execute(select(LeadSourceRow).where(LeadSourceRow.lead_id == lead_id))
        .scalars()
        .all()
    )


def describe_event(event: ContactHistory, rep_names: dict[uuid.UUID, str], current_rep_id: uuid.UUID) -> dict:
    """Render-ready view of one contact_history row for the timeline
    (§6g) and cards. Decodes each tool's content_ref format in one
    place so templates never json.loads anything.
    """
    summary = _CHANNEL_LABELS[event.channel]
    body = event.content_ref or ""
    subject = None
    sheet_link = None
    diff = None

    if event.tool == Tool.SEND_LEAD_EMAIL and event.content_ref:
        parsed = json.loads(event.content_ref)
        subject = parsed.get("subject")
        body = parsed.get("body", "")
    elif event.tool == Tool.UPDATE_LEAD_SHEET and event.content_ref:
        diff = json.loads(event.content_ref)
        body = f"{diff['field']}: {diff['current']!r} → {diff['value']!r}"
        # The one external deep link we can actually construct (§6g) —
        # source_id IS the Google file id under Decision 026.
        sheet_link = f"https://docs.google.com/spreadsheets/d/{diff['source_id']}"

    actor = None
    if event.rep_id is not None:
        actor = "you" if event.rep_id == current_rep_id else rep_names.get(event.rep_id, "another rep")

    return {
        "event_id": str(event.event_id),
        "channel": event.channel.value,
        "channel_label": summary,
        "tool": event.tool.value,
        "stage": event.stage.value,
        "outcome": event.outcome.value if event.outcome else None,
        "message_type": event.message_type.value if event.message_type else None,
        "timestamp": event.timestamp,
        "when": ago(event.timestamp),
        "actor": actor,
        "subject": subject,
        "body": body,
        "diff": diff,
        "sheet_link": sheet_link,
        "note": event.note,
        "pending_outcome": event.tool == Tool.INITIATE_LEAD_CALL
        and event.stage == Stage.EXECUTED
        and event.outcome == Outcome.PENDING,
    }


def timeline(session: Session, lead_id: uuid.UUID, current_rep_id: uuid.UUID) -> list[dict]:
    rows = (
        session.execute(
            select(ContactHistory)
            .where(ContactHistory.lead_id == lead_id)
            .order_by(ContactHistory.timestamp.desc())
        )
        .scalars()
        .all()
    )
    reps = session.execute(select(Rep)).scalars().all()
    rep_names = {r.rep_id: (r.display_name or r.email) for r in reps}
    return [describe_event(row, rep_names, current_rep_id) for row in rows]


# ---- Document checklist (Decision 008) --------------------------------
# A document only counts as present if a folder file matches its name
# keywords AND is a strict .pdf AND is >5KB — prevents false-positive
# "docs complete" handoffs on empty/invalid files.

MIN_DOC_BYTES = 5 * 1024

REQUIRED_DOCS = (
    ("Application", lambda n: "application" in n),
    ("Bank statements", lambda n: "bank" in n and "statement" in n),
    ("Prequal questionnaire", lambda n: "prequal" in n or "questionnaire" in n),
)


def doc_checklist(files: list[dict]) -> list[dict]:
    """`files` is verify_drive_contents.run() output. Returns one row
    per required doc with present/missing plus the reason a candidate
    file was rejected (truth in the interface — a file that exists but
    fails Decision 008's checks reads differently from no file at all).
    """
    results = []
    for label, matches in REQUIRED_DOCS:
        candidates = [f for f in files if matches((f.get("name") or "").lower())]
        valid = [
            f for f in candidates
            if (f.get("name") or "").lower().endswith(".pdf")
            and (f.get("size_bytes") or 0) > MIN_DOC_BYTES
        ]
        detail = None
        if valid:
            detail = valid[0]["name"]
        elif candidates:
            f = candidates[0]
            if not (f.get("name") or "").lower().endswith(".pdf"):
                detail = f"{f['name']} — not a PDF, doesn't count"
            else:
                detail = f"{f['name']} — under 5KB, doesn't count"
        results.append({"label": label, "present": bool(valid), "detail": detail})
    return results
