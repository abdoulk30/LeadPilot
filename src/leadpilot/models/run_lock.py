"""Two distinct locks, both named "run-lock" in mvp/README.md's build
order but serving different failure modes from security/threat-model.md:

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
