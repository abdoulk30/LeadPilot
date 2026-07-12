"""Per-rep Google OAuth credentials — Decision 026 (leadpilot-docs,
2026-07-11), which reverses the shared-service-account model
(Decision 024, Step 1) to per-rep OAuth (`drive.file` scope + Google
Picker). Shape sketched in architecture/state-schema.md; this is the
real implementation.

One row per rep (rep_id is the primary key) rather than a separate
UUID id with a unique constraint — Decision 026 describes a one-time
"Connect Google Account" consent per rep, i.e. a 1:1 relationship, not
one rep having multiple concurrent Google connections.

refresh_token is stored encrypted (leadpilot.crypto), never in
plaintext — see that module's docstring for the encryption approach.
"""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, func, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from leadpilot.db import Base


class RepGoogleCredential(Base):
    __tablename__ = "rep_google_credentials"

    rep_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("reps.rep_id"), primary_key=True
    )

    # Fernet-encrypted (leadpilot.crypto) — never store or log this
    # plaintext.
    refresh_token_encrypted: Mapped[str] = mapped_column(String, nullable=False)

    # Sheet/Drive file IDs the rep has selected via the Google Picker —
    # what LeadSourceConnector.list_sources() enumerates for this rep
    # (PRD v1.05 3e). Empty until the rep picks at least one file.
    granted_file_ids: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )

    connected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    # Set when the rep disconnects their Google account or access is
    # revoked. Kept as a soft-revoke rather than deleting the row so
    # there's an audit trail of when access existed.
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
