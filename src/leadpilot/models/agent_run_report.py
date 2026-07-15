"""Per-run record of what the hourly agent actually did (Step 4).

One row per (rep, run): status, the agent's final OUTPUT FORMAT JSON
(PRD v1.06 3b — the prioritized queue with rank reasons), token usage,
and the error if the run died. This is the audit surface the eval
suite and the interface's future agent-ranked queue read from — the
drafts themselves already live in contact_history (the gate), this
table holds the *judgment* (ranking, reasons, doc gaps) that would
otherwise evaporate when the run process exits.

Deliberately append-only; a re-run inserts a new row rather than
mutating the old one, matching contact_history's audit posture.
"""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, func, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from leadpilot.db import Base


class AgentRunReport(Base):
    __tablename__ = "agent_run_reports"

    run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    rep_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("reps.rep_id"), nullable=False
    )

    # running -> succeeded / failed / refused (model safety refusal) /
    # skipped_already_running (another run held this rep's lock).
    status: Mapped[str] = mapped_column(String, nullable=False, default="running")

    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # The parsed OUTPUT FORMAT payload from the agent's final message.
    report: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    error: Mapped[str | None] = mapped_column(String, nullable=True)

    model: Mapped[str | None] = mapped_column(String, nullable=True)
    iterations: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    input_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
