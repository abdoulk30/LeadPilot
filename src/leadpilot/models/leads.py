"""Canonical lead identity.

One row per real-world lead, after dedup — this is the `lead_id` that
architecture/state-schema.md's contact-history log points at ("must
survive de-dup merges... resolved after dedup, not the raw per-sheet
row id"). The mapping from raw per-sheet rows to a canonical lead_id
(the actual dedup logic) lives in LeadSourceRow, added alongside the
run-lock table — see that migration for the dedup mechanism itself.
This file only defines the canonical identity a contact-history event
or a source row can point to.
"""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, String, func, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from leadpilot.db import Base


class Lead(Base):
    __tablename__ = "leads"

    lead_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    display_name: Mapped[str | None] = mapped_column(String, nullable=True)
    primary_phone: Mapped[str | None] = mapped_column(String, nullable=True)
    primary_email: Mapped[str | None] = mapped_column(String, nullable=True)
    company: Mapped[str | None] = mapped_column(String, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
