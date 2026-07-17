"""Shared dedup/upsert logic between fetch_all_leads and
fetch_ad_hoc_sheet — extracted rather than duplicated, since both
tools do the exact same "ingest a sheet's rows into the lead system"
work (PRD v1.05 3e: "fetch_ad_hoc_sheet... isn't a new interface
method so much as a different entry point into the same
per-rep-authenticated connector"). Keeping this in one place means the
dedup heuristic can only ever be inconsistent with itself in one spot,
not two.

Dedup heuristic (Eval Case 2 — the same lead appearing on two separate
intake sheets must consolidate into one record): a fetched row with no
existing lead_source_rows entry is matched to an existing canonical
Lead by exact phone match, then exact email match, in that order. No
fuzzy matching. A row matching neither becomes a brand-new Lead.

Also the single chokepoint for Decision 006's prompt-injection
validation layer (leadpilot.injection_guard) — both fetch_all_leads
and fetch_ad_hoc_sheet funnel every fetched row through
upsert_lead_for_record before it's stored or returned, so hooking it
here protects both without duplicating the check per-tool or
per-connector. Dedup matching (find_matching_lead) runs against the
*original*, unsanitized phone/email first, before sanitization mutates
them — using the placeholder value for matching would make two
unrelated flagged rows (different attacker, different sheet, same
fixed placeholder string) collide into a single fabricated "lead" with
no real phone or email in common. record.raw is deliberately never
sanitized — it's an internal change-detection snapshot
(detect_changes/LeadSourceRow.raw_data), never returned to a tool
caller or shown to the agent, so it isn't part of this threat's actual
attack surface.
"""

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from leadpilot import injection_alerts, injection_guard
from leadpilot.connectors.base import LeadRecord
from leadpilot.models.dedup import LeadSourceRow
from leadpilot.models.leads import Lead


def find_matching_lead(session: Session, phone: str | None, email: str | None) -> Lead | None:
    if phone:
        existing = session.execute(select(Lead).where(Lead.primary_phone == phone)).scalars().first()
        if existing:
            return existing
    if email:
        existing = session.execute(select(Lead).where(Lead.primary_email == email)).scalars().first()
        if existing:
            return existing
    return None


def upsert_lead_for_record(session: Session, rep_id: uuid.UUID, source_id: str, record: LeadRecord) -> uuid.UUID:
    original_phone, original_email = record.phone, record.email
    flagged_reasons = injection_guard.sanitize_record_in_place(record)
    if flagged_reasons:
        injection_alerts.record_incident_and_maybe_notify(
            session, rep_id, source_id, record.row_ref, flagged_reasons,
        )

    existing_row = session.execute(
        select(LeadSourceRow).where(
            LeadSourceRow.source_id == source_id, LeadSourceRow.row_ref == record.row_ref
        )
    ).scalar_one_or_none()

    if existing_row is not None:
        if existing_row.raw_data != record.raw:
            existing_row.raw_data = record.raw
        return existing_row.lead_id

    matching_lead = find_matching_lead(session, original_phone, original_email)
    if matching_lead is not None:
        lead_id = matching_lead.lead_id
        # Enrichment on dedup (Marc, 2026-07-15): the same person often
        # appears on an intake sheet with no company column AND a
        # fuller sheet with Business Name etc. Fill the canonical
        # lead's *blank* fields from the new source — never overwrite
        # a value that's already there (first source stays
        # authoritative for conflicts; this only closes gaps).
        for lead_attr, record_value in (
            ("display_name", record.name),
            ("primary_phone", record.phone),
            ("primary_email", record.email),
            ("company", record.company),
        ):
            if getattr(matching_lead, lead_attr) is None and record_value:
                setattr(matching_lead, lead_attr, record_value)
    else:
        new_lead = Lead(
            display_name=record.name,
            primary_phone=record.phone,
            primary_email=record.email,
            company=record.company,
        )
        session.add(new_lead)
        session.flush()
        lead_id = new_lead.lead_id

    session.add(LeadSourceRow(source_id=source_id, row_ref=record.row_ref, lead_id=lead_id, raw_data=record.raw))
    return lead_id


def record_to_dict(source_id: str, lead_id: uuid.UUID, record: LeadRecord) -> dict:
    """The shared per-row output shape both tools return — PRD v1.05
    3a: fetch_ad_hoc_sheet returns "the same shape as fetch_all_leads's
    per-row output." Called after upsert_lead_for_record has already
    sanitized record in place, so `flagged` here is just "does any
    guarded field equal the placeholder" — reliable because
    FLAGGED_PLACEHOLDER is a fixed, specific string no legitimate cell
    value would coincidentally match, not a heuristic re-check.
    """
    return {
        "lead_id": str(lead_id),
        "source_id": source_id,
        "row_ref": record.row_ref,
        "name": record.name,
        "phone": record.phone,
        "email": record.email,
        "company": record.company,
        "status": record.status,
        "flagged": injection_guard.FLAGGED_PLACEHOLDER in (
            record.name, record.phone, record.email, record.company, record.status,
        ),
    }
