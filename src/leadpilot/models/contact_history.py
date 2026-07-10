"""Contact-history log + approval gate.

Matches leadpilot-docs/architecture/state-schema.md exactly: one
append-only row per contact-related event, with `stage` mutated in
place as the row moves through its lifecycle. This `stage` field is
not just a record — per Decision 021, it *is* the rep-approval
enforcement mechanism. The real effect of a staged action is only
permitted to fire after a single atomic conditional update:

    UPDATE contact_history SET stage = 'executed'
    WHERE event_id = :event_id AND stage = 'approved'

If that update affects zero rows, nothing runs. See
gate.try_execute() for the actual implementation of that query.
"""

import enum
import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, String, func, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy import Enum as SAEnum

from leadpilot.db import Base


def _pg_enum(python_enum: type[enum.Enum], name: str) -> SAEnum:
    """SQLAlchemy's Enum type stores the Python member *name*
    (e.g. "CALL") by default, not its .value (e.g. "call") — that
    would silently diverge from the lowercase values documented in
    architecture/state-schema.md. values_callable forces it to use
    .value instead, so the actual Postgres enum labels match the docs.
    """
    return SAEnum(python_enum, name=name, values_callable=lambda e: [m.value for m in e])


class Channel(str, enum.Enum):
    CALL = "call"
    TEXT = "text"
    EMAIL = "email"
    SLACK_HANDOFF = "slack_handoff"
    SHEET_EDIT = "sheet_edit"


class Tool(str, enum.Enum):
    INITIATE_LEAD_CALL = "initiate_lead_call"
    SEND_LEAD_TEXT = "send_lead_text"
    SEND_LEAD_EMAIL = "send_lead_email"
    DISPATCH_SLACK_HANDOFF = "dispatch_slack_handoff"
    UPDATE_LEAD_SHEET = "update_lead_sheet"


class Stage(str, enum.Enum):
    DRAFTED = "drafted"
    AWAITING_REP_APPROVAL = "awaiting_rep_approval"
    APPROVED = "approved"
    EXECUTED = "executed"
    REJECTED = "rejected"
    EXPIRED = "expired"


class Outcome(str, enum.Enum):
    # send_lead_text / send_lead_email / dispatch_slack_handoff get a
    # real delivery outcome from their provider APIs.
    DELIVERED = "delivered"
    FAILED = "failed"
    # initiate_lead_call only ever gets "pending" automatically —
    # closing the loop requires the rep to call log_call_outcome
    # (PRD v1.04 3a/3f). Normalized from the PRD's "didn't_call" to
    # avoid an apostrophe in a stored enum literal.
    PENDING = "pending"
    ANSWERED = "answered"
    NO_ANSWER = "no_answer"
    VOICEMAIL = "voicemail"
    DIDNT_CALL = "didnt_call"


class ContactHistory(Base):
    __tablename__ = "contact_history"

    event_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )

    lead_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("leads.lead_id"), nullable=False
    )

    channel: Mapped[Channel] = mapped_column(_pg_enum(Channel, "channel"), nullable=False)
    tool: Mapped[Tool] = mapped_column(_pg_enum(Tool, "tool"), nullable=False)
    stage: Mapped[Stage] = mapped_column(
        _pg_enum(Stage, "stage"), nullable=False, default=Stage.DRAFTED
    )

    # "When this stage transition happened" per state-schema.md — this
    # is deliberately mutated on every transition, not a fixed
    # creation timestamp. Flagged to Abdoul: there's no immutable
    # created_at in the documented schema, so "how long has this sat
    # in awaiting_rep_approval" isn't derivable from this row alone
    # once it moves past that stage. Not adding one unilaterally since
    # it's not in the spec — raise if you want it added.
    timestamp: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    rep_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("reps.rep_id"), nullable=True
    )

    outcome: Mapped[Outcome | None] = mapped_column(_pg_enum(Outcome, "outcome"), nullable=True)
    content_ref: Mapped[str | None] = mapped_column(String, nullable=True)
    note: Mapped[str | None] = mapped_column(String, nullable=True)

    __table_args__ = (
        Index("ix_contact_history_lead_id", "lead_id"),
        Index("ix_contact_history_timestamp", "timestamp"),
    )
