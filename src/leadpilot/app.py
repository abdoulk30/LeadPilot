"""Minimal FastAPI app — just enough to prove the rep-session system
works end-to-end over real HTTP, not just as isolated function calls.
The actual rep-facing dashboard (prioritized queue, approve/reject,
diff view, etc.) is Step 3 — this only covers Step 1's authentication
piece: login, logout, and a protected endpoint demonstrating the
AUTHENTICATION GUARD from PRD v1.04's system prompt.
"""

from collections.abc import Generator

from fastapi import Cookie, Depends, FastAPI, HTTPException, Response
from pydantic import BaseModel
from sqlalchemy.orm import Session

from leadpilot import auth
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
