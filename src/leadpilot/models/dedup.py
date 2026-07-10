"""Dedup mapping: raw per-sheet rows -> canonical lead.

Eval Case 2 (PRD v1.04 3d): the same lead can appear on two separate
intake spreadsheets with differing source annotations, and must be
consolidated into a single record. This table is the structural
mapping that makes that possible — one row per raw source row, pointed
at whichever canonical `leads.lead_id` it resolves to.

The actual matching heuristic (deciding *which* canonical lead a new
raw row belongs to — by phone, email, name, or some combination) is
Step 2 business logic (fetch_all_leads), not schema. This file only
represents the result of that decision, plus what LeadSourceConnector
needs for `detect_changes` (PRD v1.04 3e): a raw snapshot and a
last-seen timestamp per row.
"""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, UniqueConstraint, func, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from leadpilot.db import Base


class LeadSourceRow(Base):
    __tablename__ = "lead_source_rows"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )

    # Matches LeadSourceConnector.list_sources()/fetch_rows(source_id)
    # (PRD v1.04 3e) — which configured spreadsheet this row came from.
    source_id: Mapped[str] = mapped_column(String, nullable=False)
    # The sheet-native row reference (e.g. row number, or a stable
    # per-row id if the sheet has one) — opaque to this table, owned
    # by GoogleSheetsConnector.
    row_ref: Mapped[str] = mapped_column(String, nullable=False)

    lead_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("leads.lead_id"), nullable=False
    )

    # Raw fetched row snapshot, for detect_changes() to diff against
    # on the next run.
    raw_data: Mapped[dict] = mapped_column(JSONB, nullable=False)

    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (UniqueConstraint("source_id", "row_ref", name="uq_lead_source_row"),)
