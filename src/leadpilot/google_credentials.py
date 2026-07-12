"""Store/retrieve/revoke each rep's Google OAuth credentials — Decision
026's per-rep model. Encryption (leadpilot.crypto) is handled here,
not in the model or the OAuth callback route, so there's exactly one
place that ever touches a refresh token's plaintext.
"""

import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from leadpilot.crypto import decrypt, encrypt
from leadpilot.models.rep_google_credential import RepGoogleCredential


def store_credential(session: Session, rep_id: uuid.UUID, refresh_token: str) -> None:
    """Called from the OAuth callback after exchanging an auth code for
    tokens. Upserts — a rep reconnecting (e.g. after revoking access on
    Google's side) overwrites the old encrypted token and clears
    revoked_at, rather than erroring on the existing primary-key row.
    Does not touch granted_file_ids; the Picker grants files in a
    separate step.
    """
    encrypted = encrypt(refresh_token)
    stmt = (
        insert(RepGoogleCredential)
        .values(rep_id=rep_id, refresh_token_encrypted=encrypted)
        .on_conflict_do_update(
            index_elements=[RepGoogleCredential.rep_id],
            set_={"refresh_token_encrypted": encrypted, "revoked_at": None},
        )
    )
    session.execute(stmt)


def get_refresh_token(session: Session, rep_id: uuid.UUID) -> str | None:
    """Decrypted refresh token for this rep, or None if they've never
    connected or have since been revoked. Never log or return this
    where it could end up in the contact-history log's content_ref —
    it's a credential, not draft content.
    """
    row = session.execute(
        select(RepGoogleCredential).where(
            RepGoogleCredential.rep_id == rep_id,
            RepGoogleCredential.revoked_at.is_(None),
        )
    ).scalar_one_or_none()
    if row is None:
        return None
    return decrypt(row.refresh_token_encrypted)


def granted_file_ids(session: Session, rep_id: uuid.UUID) -> list[str]:
    """What GoogleSheetsConnector.list_sources() enumerates for this
    rep (PRD v1.05 3e). Empty list if never connected/revoked, not an
    error — callers should treat that as "no sources," matching
    fetch_all_leads's existing empty-sheet handling.
    """
    row = session.execute(
        select(RepGoogleCredential).where(
            RepGoogleCredential.rep_id == rep_id,
            RepGoogleCredential.revoked_at.is_(None),
        )
    ).scalar_one_or_none()
    return list(row.granted_file_ids) if row else []


def add_granted_file(session: Session, rep_id: uuid.UUID, file_id: str) -> None:
    """Record a file the rep just selected via the Google Picker.
    Read-modify-write, not an atomic upsert like locks.py — this is a
    single rep's own UI action, not a contended resource multiple
    concurrent processes race over, so the stricter pattern used for
    the approval gate/locks isn't needed here. Deduplicates rather than
    appending blindly, since re-picking an already-granted file (or a
    retried request) shouldn't grow the list.
    """
    row = session.execute(
        select(RepGoogleCredential).where(RepGoogleCredential.rep_id == rep_id)
    ).scalar_one()
    if file_id not in row.granted_file_ids:
        row.granted_file_ids = [*row.granted_file_ids, file_id]


def revoke(session: Session, rep_id: uuid.UUID) -> bool:
    """Soft-revoke — the row stays (audit trail of when access
    existed), but get_refresh_token/granted_file_ids treat it as
    disconnected. Returns False if the rep never had a credential row
    to revoke.
    """
    row = session.execute(
        select(RepGoogleCredential).where(RepGoogleCredential.rep_id == rep_id)
    ).scalar_one_or_none()
    if row is None:
        return False
    row.revoked_at = datetime.now(timezone.utc)
    return True
