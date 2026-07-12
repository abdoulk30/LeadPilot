"""Minimal FastAPI app — just enough to prove the rep-session system
works end-to-end over real HTTP, not just as isolated function calls.
The actual rep-facing dashboard (prioritized queue, approve/reject,
diff view, etc.) is Step 3 — this only covers Step 1's authentication
piece: login, logout, and a protected endpoint demonstrating the
AUTHENTICATION GUARD from PRD v1.04's system prompt.
"""

from collections.abc import Generator

from fastapi import Cookie, Depends, FastAPI, HTTPException, Response
from fastapi.responses import RedirectResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from leadpilot import auth, google_credentials, google_oauth
from leadpilot.db import SessionLocal
from leadpilot.models.rep import Rep

app = FastAPI(title="LeadPilot")

SESSION_COOKIE_NAME = "leadpilot_session"


def get_db() -> Generator[Session, None, None]:
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


def require_rep(
    leadpilot_session: str | None = Cookie(default=None),
    db: Session = Depends(get_db),
) -> Rep:
    """The AUTHENTICATION GUARD, as an actual FastAPI dependency.
    PRD v1.04: 'If invoked outside a valid session, do not return lead
    or contact data — log the attempt and take no further action.'
    Every endpoint that touches lead/contact data should depend on
    this rather than reading the cookie itself.
    """
    if leadpilot_session is None:
        raise HTTPException(status_code=401, detail="Not authenticated")
    rep = auth.get_rep_for_signed_token(db, leadpilot_session)
    if rep is None:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return rep


class LoginRequest(BaseModel):
    email: str
    password: str


class RepOut(BaseModel):
    rep_id: str
    email: str
    display_name: str | None


@app.post("/login")
def login(payload: LoginRequest, response: Response, db: Session = Depends(get_db)):
    rep = auth.authenticate(db, email=payload.email, password=payload.password)
    if rep is None:
        db.commit()  # persist the failed-attempt log even though nothing else changed
        raise HTTPException(status_code=401, detail="Invalid email or password")
    signed_token = auth.create_session(db, rep.rep_id)
    db.commit()
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=signed_token,
        httponly=True,
        samesite="lax",
        max_age=int(auth.DEFAULT_SESSION_TTL.total_seconds()),
    )
    return {"rep_id": str(rep.rep_id), "email": rep.email}


@app.post("/logout")
def logout(
    response: Response,
    leadpilot_session: str | None = Cookie(default=None),
    db: Session = Depends(get_db),
):
    if leadpilot_session is not None:
        auth.revoke_session(db, leadpilot_session)
        db.commit()
    response.delete_cookie(SESSION_COOKIE_NAME)
    return {"logged_out": True}


@app.get("/whoami", response_model=RepOut)
def whoami(rep: Rep = Depends(require_rep)):
    return RepOut(rep_id=str(rep.rep_id), email=rep.email, display_name=rep.display_name)


GOOGLE_OAUTH_STATE_COOKIE = "leadpilot_google_oauth_state"


@app.get("/auth/google/connect")
def google_connect(response: Response, rep: Rep = Depends(require_rep)):
    """Step 1 of the OAuth round trip. Requires an already-logged-in
    rep (Decision 013's session, not Google's) — connecting a Google
    account is something an authenticated rep does, not a login method
    itself (see Decision 023: rep login stays email+password).
    """
    state = google_oauth.generate_state()
    auth_url = google_oauth.build_authorization_url(state)
    redirect = RedirectResponse(url=auth_url)
    redirect.set_cookie(
        key=GOOGLE_OAUTH_STATE_COOKIE,
        value=state,
        httponly=True,
        samesite="lax",
        max_age=google_oauth.STATE_MAX_AGE_SECONDS,
    )
    return redirect


@app.get("/auth/google/callback")
def google_callback(
    code: str,
    state: str,
    response: Response,
    leadpilot_google_oauth_state: str | None = Cookie(default=None),
    rep: Rep = Depends(require_rep),
    db: Session = Depends(get_db),
):
    if not google_oauth.verify_state(leadpilot_google_oauth_state, state):
        raise HTTPException(status_code=400, detail="Invalid or expired OAuth state — try connecting again")
    response.delete_cookie(GOOGLE_OAUTH_STATE_COOKIE)

    try:
        refresh_token = google_oauth.exchange_code_for_refresh_token(code)
    except Exception as e:
        # Real failure from Google's token endpoint (bad/expired code,
        # revoked client, etc.) — surface it rather than pretending
        # the connection succeeded.
        raise HTTPException(status_code=400, detail=f"Google token exchange failed: {e}") from e

    google_credentials.store_credential(db, rep.rep_id, refresh_token)
    db.commit()
    return {"connected": True, "rep_id": str(rep.rep_id)}


@app.get("/auth/google/access-token")
def google_access_token(rep: Rep = Depends(require_rep), db: Session = Depends(get_db)):
    """For the Google Picker widget (Step 3) to call right before
    opening the picker — a fresh, short-lived access token, minted on
    demand from the rep's stored refresh token. Never the refresh
    token itself; never cached here.
    """
    token = google_oauth.get_fresh_access_token(db, rep.rep_id)
    if token is None:
        raise HTTPException(status_code=404, detail="Rep has not connected a Google account")
    return {"access_token": token}
