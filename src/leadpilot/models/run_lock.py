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

2. AgentRunLock — a per-rep mutex (reworked from a singleton, Decision
   027/032) so the same rep's hourly batch run can't overlap with
   itself (e.g. a slow run still executing when the next scheduled
   trigger fires) — while rep A's run and rep B's run proceed fully
   independently, since the batch job now iterates once per connected
   rep rather than running once globally (Decision 027). Without this,
   two overlapping runs for the same rep could both draft outreach for
   the same lead before either one's lead-action lock check would
   catch the other.
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

    # rep_id, not a fixed string id (Decision 027/032's per-rep rework)
    # — one row per rep who's ever had a batch run attempt, rather than
    # one global row. Two reps' runs never contend for the same row.
    rep_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("reps.rep_id"), primary_key=True
    )
    locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    locked_by: Mapped[str | None] = mapped_column(String, nullable=True)
