"""Rep authentication — Decision 013, resolved as email+password with
our own DB-backed sessions (confirmed with Abdoul 2026-07-09).

This is the actual enforcement point for PRD v1.04's AUTHENTICATION
GUARD: "Only operate within an authenticated, authorized rep session.
If invoked outside a valid session, do not return lead or contact
data — log the attempt and take no further action." See
require_rep() in app.py for where this gets wired into request
handling.
"""

import logging
import secrets
import uuid
from datetime import datetime, timedelta, timezone

import bcrypt
from itsdangerous import BadSignature, URLSafeTimedSerializer
from sqlalchemy import select
from sqlalchemy.orm import Session

from leadpilot.config import settings
from leadpilot.models.rep import Rep, RepSession

logger = logging.getLogger("leadpilot.auth")

DEFAULT_SESSION_TTL = timedelta(hours=12)
_BCRYPT_MAX_PASSWORD_BYTES = 72


def _serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(settings.rep_auth_session_secret, salt="rep-session")


def hash_password(password: str) -> str:
    raw = password.encode("utf-8")
    if len(raw) > _BCRYPT_MAX_PASSWORD_BYTES:
        # bcrypt silently ignores bytes past 72 rather than raising in
        # some builds — reject explicitly instead of hashing something
        # shorter than the rep thinks their password is.
        raise ValueError(f"Password too long ({len(raw)} bytes; bcrypt supports up to {_BCRYPT_MAX_PASSWORD_BYTES})")
    return bcrypt.hashpw(raw, bcrypt.gensalt()).decode("utf-8")


def verify_password(password: str, password_hash: str) -> bool:
    return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8"))


def create_rep(session: Session, *, email: str, password: str, display_name: str | None = None) -> Rep:
    rep = Rep(email=email.lower().strip(), password_hash=hash_password(password), display_name=display_name)
    session.add(rep)
    session.flush()
    return rep


def authenticate(session: Session, *, email: str, password: str) -> Rep | None:
    rep = session.execute(select(Rep).where(Rep.email == email.lower().strip())).scalar_one_or_none()
    if rep is None or not rep.is_active:
        # Deliberately the same "failed" path whether the email
        # doesn't exist or the account is deactivated — don't leak
        # which case it was.
        logger.warning("authentication failed: unknown or inactive email=%r", email)
        return None
    if not verify_password(password, rep.password_hash):
        logger.warning("authentication failed: bad password for rep_id=%s", rep.rep_id)
        return None
    return rep


def create_session(session: Session, rep_id: uuid.UUID, ttl: timedelta = DEFAULT_SESSION_TTL) -> str:
    """Creates the DB-backed session row and returns the signed cookie
    value to hand back to the rep's browser. The signature (itsdangerous,
    REP_AUTH_SESSION_SECRET) is a cheap tamper/integrity check on top of
    the session_id's own 256 bits of randomness — the real revocability
    and expiry enforcement happens against the DB row, not the signature.
    """
    session_id = secrets.token_urlsafe(32)
    now = datetime.now(timezone.utc)
    session.add(RepSession(session_id=session_id, rep_id=rep_id, expires_at=now + ttl))
    session.flush()
    return _serializer().dumps(session_id)


def get_rep_for_signed_token(session: Session, signed_token: str) -> Rep | None:
    """Full validation chain: signature -> DB row exists -> not revoked
    -> not expired -> rep still active. Returns None (never raises) on
    any failure so callers have one uniform "not authenticated" path —
    matching the AUTHENTICATION GUARD's "do not return lead or contact
    data" requirement regardless of which specific check failed.
    """
    try:
        session_id = _serializer().loads(signed_token)
    except BadSignature:
        logger.warning("authentication rejected: invalid session cookie signature")
        return None

    rep_session = session.execute(
        select(RepSession).where(RepSession.session_id == session_id)
    ).scalar_one_or_none()
    if rep_session is None:
        logger.warning("authentication rejected: unknown session_id")
        return None
    if rep_session.revoked_at is not None:
        logger.warning("authentication rejected: revoked session rep_id=%s", rep_session.rep_id)
        return None
    if rep_session.expires_at < datetime.now(timezone.utc):
        logger.warning("authentication rejected: expired session rep_id=%s", rep_session.rep_id)
        return None

    rep = session.get(Rep, rep_session.rep_id)
    if rep is None or not rep.is_active:
        logger.warning("authentication rejected: inactive/missing rep for session rep_id=%s", rep_session.rep_id)
        return None
    return rep


def get_login_time_for_signed_token(session: Session, signed_token: str) -> datetime | None:
    """The exact RepSession row's created_at for this specific cookie —
    used by the injection-alert settings panel to decide what counts as
    "since you logged in fresh": a display-only cutoff, deliberately not
    a real reset of the underlying rate-limiter state (see
    leadpilot.injection_alerts) — resetting the real state on login
    would let a rep dodge the 1-hour suppression cooldown just by
    logging out and back in. Returns None on any invalid/expired token,
    same "never raise" contract as get_rep_for_signed_token.
    """
    try:
        session_id = _serializer().loads(signed_token)
    except BadSignature:
        return None
    rep_session = session.execute(
        select(RepSession).where(RepSession.session_id == session_id)
    ).scalar_one_or_none()
    return rep_session.created_at if rep_session is not None else None


def revoke_session(session: Session, signed_token: str) -> bool:
    try:
        session_id = _serializer().loads(signed_token, max_age=None)
    except BadSignature:
        return False
    result = session.execute(
        select(RepSession).where(RepSession.session_id == session_id, RepSession.revoked_at.is_(None))
    )
    rep_session = result.scalar_one_or_none()
    if rep_session is None:
        return False
    rep_session.revoked_at = datetime.now(timezone.utc)
    session.flush()
    return True
