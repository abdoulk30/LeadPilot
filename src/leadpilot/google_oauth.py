"""Google OAuth for the per-rep access model (Decision 026).

Two things this deliberately does NOT do, worth stating up front:
- It never hands a refresh token to the browser. Only ever stored
  server-side (encrypted — see google_credentials.py/crypto.py).
- It never puts any token in a URL (query string, redirect fragment).
  Access tokens are short-lived (~1 hour) but URLs end up in server
  logs and browser history, so get_fresh_access_token() is exposed as
  its own authenticated JSON endpoint (app.py) instead — the frontend
  (Step 3's Picker integration) fetches a token right before it's
  needed rather than carrying one around.

Scope was drive.file only through Decision 026 — LeadPilot only ever
saw files a rep explicitly selected via the Google Picker, never their
whole Drive. Decision 033 added drive.readonly on top of that: the
drive.file per-item grant turned out not to extend to a folder's
contents (confirmed against the real API — granting a folder via
Picker does not grant visibility into files added to, or already
sitting in, that folder), which made verify_drive_contents unable to
do its actual job. drive.file is kept for the write path
(update_lead_sheet's commit_field_write) and for the deliberate
per-item consent UX on fetch_all_leads/fetch_ad_hoc_sheet;
drive.readonly is what verify_drive_contents actually reads through.
See Decision 033 for the full tradeoff and the note to revisit this
for a narrower alternative later.

access_type=offline + prompt=consent on the authorization URL
guarantees Google actually returns a refresh_token on every connect,
not just the first one — without prompt=consent, a rep reconnecting
after a prior consent can get an access-token-only response with no
refresh_token in it, silently breaking storage. Widening SCOPES here
means every rep who connected before Decision 033 is holding a
refresh token that does NOT cover drive.readonly — they must
reconnect (redo the Connect Google Account flow) before
verify_drive_contents will work for them; there's no way to silently
upgrade an already-issued token's scope.
"""

import secrets
import uuid

from google.auth.transport.requests import Request as GoogleAuthRequest
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from sqlalchemy.orm import Session

from leadpilot import google_credentials
from leadpilot.config import settings

SCOPES = [
    "https://www.googleapis.com/auth/drive.file",
    "https://www.googleapis.com/auth/drive.readonly",
]

# The OAuth round trip should complete in well under this; a long
# window just gives a stolen/replayed state value more time to matter.
STATE_MAX_AGE_SECONDS = 600


def _state_serializer() -> URLSafeTimedSerializer:
    # Reuses REP_AUTH_SESSION_SECRET rather than introducing a second
    # secret to manage — different salt keeps this namespace distinct
    # from session-cookie signing (leadpilot.auth), so a state value
    # can never be replayed as a session token or vice versa.
    return URLSafeTimedSerializer(settings.rep_auth_session_secret, salt="google-oauth-state")


def generate_state() -> str:
    """A random nonce, signed. The signed value is both set as an
    httponly cookie and passed as the `state` query param to Google;
    callback() only proceeds if they match — standard OAuth CSRF
    defense (confirms this callback resulted from a request this
    browser actually made, not a forged one).
    """
    nonce = secrets.token_urlsafe(32)
    return _state_serializer().dumps(nonce)


def verify_state(cookie_value: str | None, query_value: str | None) -> bool:
    if not cookie_value or not query_value or cookie_value != query_value:
        return False
    try:
        _state_serializer().loads(cookie_value, max_age=STATE_MAX_AGE_SECONDS)
    except (BadSignature, SignatureExpired):
        return False
    return True


def _pkce_serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(settings.rep_auth_session_secret, salt="google-oauth-pkce")


def sign_code_verifier(code_verifier: str) -> str:
    """google-auth-oauthlib's Flow generates a PKCE code_verifier
    on-instance inside authorization_url() (autogenerate_code_verifier
    defaults to True) and expects the *same* Flow object back for
    fetch_token(). Since /connect and /callback are two separate HTTP
    requests — likely two different Flow instances entirely — the
    verifier has to be carried across them explicitly, the same way
    `state` already is. Signed for the same reason state is: a cheap
    tamper/expiry check on top of the cookie.
    """
    return _pkce_serializer().dumps(code_verifier)


def _unsign_code_verifier(signed_value: str) -> str | None:
    try:
        return _pkce_serializer().loads(signed_value, max_age=STATE_MAX_AGE_SECONDS)
    except (BadSignature, SignatureExpired):
        return None


def _client_config() -> dict:
    return {
        "web": {
            "client_id": settings.google_oauth_client_id,
            "client_secret": settings.google_oauth_client_secret,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": [settings.google_oauth_redirect_uri],
        }
    }


def _flow() -> Flow:
    flow = Flow.from_client_config(_client_config(), scopes=SCOPES)
    flow.redirect_uri = settings.google_oauth_redirect_uri
    return flow


def build_authorization_url(state: str) -> tuple[str, str]:
    """Returns (authorization_url, signed_code_verifier). The caller
    (app.py) must set the signed verifier as a cookie, the same way it
    already does for state — see sign_code_verifier's docstring for why.
    """
    flow = _flow()
    url, _ = flow.authorization_url(
        access_type="offline",
        prompt="consent",
        state=state,
        include_granted_scopes="true",
    )
    return url, sign_code_verifier(flow.code_verifier)


def exchange_code_for_refresh_token(code: str, signed_code_verifier: str) -> str:
    """Real call to Google's token endpoint. Raises whatever
    google-auth-oauthlib raises (e.g. on an invalid/expired code, or a
    missing/expired code_verifier) — callers (app.py) turn that into an
    HTTP error, not swallow it.
    """
    code_verifier = _unsign_code_verifier(signed_code_verifier)
    if code_verifier is None:
        raise ValueError("Missing or expired PKCE code_verifier — try connecting again")

    flow = _flow()
    flow.code_verifier = code_verifier
    flow.fetch_token(code=code)
    refresh_token = flow.credentials.refresh_token
    if not refresh_token:
        # Should not happen given access_type=offline + prompt=consent
        # above, but fail loudly rather than storing an empty token
        # that would silently break every later use.
        raise ValueError(
            "Google did not return a refresh_token. "
            "Check that the authorization URL included access_type=offline and prompt=consent."
        )
    return refresh_token


def get_fresh_access_token(session: Session, rep_id: uuid.UUID) -> str | None:
    """Uses the rep's stored refresh token to mint a short-lived access
    token on demand, via a real call to Google's token endpoint — not
    a cached/reused value. Returns None if the rep hasn't connected
    (or has been revoked), so callers can distinguish "not connected"
    from a real error.
    """
    refresh_token = google_credentials.get_refresh_token(session, rep_id)
    if refresh_token is None:
        return None
    creds = Credentials(
        token=None,
        refresh_token=refresh_token,
        token_uri="https://oauth2.googleapis.com/token",
        client_id=settings.google_oauth_client_id,
        client_secret=settings.google_oauth_client_secret,
        scopes=SCOPES,
    )
    creds.refresh(GoogleAuthRequest())
    return creds.token
