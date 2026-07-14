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
"""

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

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


def upsert_lead_for_record(session: Session, source_id: str, record: LeadRecord) -> uuid.UUID:
    existing_row = session.execute(
        select(LeadSourceRow).where(
            LeadSourceRow.source_id == source_id, LeadSourceRow.row_ref == record.row_ref
        )
    ).scalar_one_or_none()

    if existing_row is not None:
        if existing_row.raw_data != record.raw:
            existing_row.raw_data = record.raw
        return existing_row.lead_id

    matching_lead = find_matching_lead(session, record.phone, record.email)
    if matching_lead is not None:
        lead_id = matching_lead.lead_id
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
    per-row output."
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
    }
