"""Reps and their sessions — Decision 013 (access control), resolved
as email+password with our own server-side sessions (confirmed with
Abdoul 2026-07-09) rather than Google OAuth, to keep this work
independent of Marc's Step 0 Google Cloud project.

Sessions are DB-backed (a dedicated table, not a stateless JWT) so a
session can actually be revoked — logout, or a rep being deactivated,
takes effect immediately rather than waiting for a token to expire.
The cookie itself is still signed (see leadpilot.auth) as a cheap
extra integrity layer, matching what REP_AUTH_SESSION_SECRET was
already documented to do in .env.example.
"""

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, String, func, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from leadpilot.db import Base


class Rep(Base):
    __tablename__ = "reps"

    rep_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    email: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    password_hash: Mapped[str] = mapped_column(String, nullable=False)
    display_name: Mapped[str | None] = mapped_column(String, nullable=True)
    # Deactivating a rep (instead of deleting) preserves their history
    # in contact_history.rep_id while blocking future logins/approvals.
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class RepSession(Base):
    __tablename__ = "rep_sessions"

    # The opaque token stored (signed) in the rep's session cookie.
    session_id: Mapped[str] = mapped_column(String, primary_key=True)
    rep_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("reps.rep_id"), nullable=False)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
