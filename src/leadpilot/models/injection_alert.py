"""Rep-facing email alerting for injection_guard incidents (chat
request, 2026-07-17) — a durable append-only log plus a small per-rep
rate-limiter state row, same one-row-per-key convention as
run_lock.py's three locks.

InjectionIncident is the source of truth: one row per flagged sheet
row (all flagged fields on that row bundled into one incident/email,
not one row per field — a single malicious row shouldn't burn through
the burst budget by itself). Logged unconditionally, regardless of the
rep's alert setting or the rate limiter's state, so nothing is lost to
"examine later" just because notifications were off or suppressed.

RepInjectionAlertState is the only mutable state: the trailing-5-minute
burst count is a live COUNT query against InjectionIncident, not a
second counter that could drift out of sync with the log — see
leadpilot.injection_alerts for the actual state-machine logic.
"""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, String
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import text

from leadpilot.db import Base


class InjectionIncident(Base):
    __tablename__ = "injection_incidents"

    incident_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    rep_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("reps.rep_id"), nullable=False)
    source_id: Mapped[str] = mapped_column(String, nullable=False)
    row_ref: Mapped[str] = mapped_column(String, nullable=False)
    # {field_name: reason} — every guarded field flagged on this row,
    # from injection_guard.sanitize_record_in_place's return value.
    reasons: Mapped[dict[str, str]] = mapped_column(JSONB, nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        # The burst-window count query filters on (rep_id, occurred_at)
        # every single incident — worth a real index rather than a scan.
        Index("ix_injection_incidents_rep_occurred", "rep_id", "occurred_at"),
    )


class RepInjectionAlertState(Base):
    __tablename__ = "rep_injection_alert_state"

    rep_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("reps.rep_id"), primary_key=True)
    # Non-null while the burst breaker is open (5+ incidents in the
    # trailing 5 minutes already alerted on). Cleared the next time an
    # incident arrives at least an hour after the previous one — see
    # injection_alerts._evaluate — never swept on a timer.
    tripped_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_incident_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_email_sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
