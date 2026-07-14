"""Three distinct locks, serving three distinct failure modes from
security/threat-model.md:

1. LeadActionLock — per-lead duplicate-contact prevention. The named
   threat: "Two run cycles triggered in rapid succession against the
   same lead... a hot lead is dialed or texted twice." One row per
   lead; `last_action_committed_at` is checked and atomically updated
   *before* a new outreach draft is created for that lead (see
   leadpilot.locks.try_acquire_lead_action_lock) — Step 2 business
   logic decides the actual cooldown window, this table just holds the
   timestamp.

2. AgentRunLock — a singleton mutex so the hourly Cron Job can't run
   twice concurrently (e.g. a slow run still executing when the next
   scheduled trigger fires). Not explicitly named in the PRD, but
   implied by "atomic state locking" and needed for the same reason
   the per-lead lock is: without it, two overlapping batch runs could
   both draft outreach for the same lead before either one's lock
   check would catch the other.

3. SheetCellLock — added Decision 034, closing a real race condition
   found in `GoogleSheetsConnector.commit_field_write`: two reps
   (or the same rep's overlapping runs) approving edits to the same
   spreadsheet cell around the same time could silently clobber each
   other, since a plain Sheets API `values().update()` has no
   built-in compare-and-swap. One row per in-flight write, keyed by a
   `"{source_id}:{row_ref}:{field_name}"` string rather than a FK,
   since the target isn't a LeadPilot-owned row — see
   leadpilot.locks.try_acquire_sheet_cell_lock /
   release_sheet_cell_lock, and connectors/base.py's
   commit_field_write docstring for how this pairs with the
   *separate* optimistic expected-value check (this lock only
   protects against concurrent LeadPilot-originated commits; it can't
   catch someone editing the sheet directly in Google's own UI, which
   is what the expected-value check is for).
"""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from leadpilot.db import Base


class LeadActionLock(Base):
    __tablename__ = "lead_action_locks"

    lead_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("leads.lead_id"), primary_key=True
    )
    last_action_committed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class AgentRunLock(Base):
    __tablename__ = "agent_run_locks"

    # Fixed id — one row per named lock. Only "hourly_batch_run" exists
    # today; the column exists so a second named lock could be added
    # later without a schema change.
    id: Mapped[str] = mapped_column(String, primary_key=True)
    locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    locked_by: Mapped[str | None] = mapped_column(String, nullable=True)


class SheetCellLock(Base):
    __tablename__ = "sheet_cell_locks"

    # "{source_id}:{row_ref}:{field_name}" — see module docstring.
    cell_key: Mapped[str] = mapped_column(String, primary_key=True)
    locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    locked_by: Mapped[str | None] = mapped_column(String, nullable=True)
