"""Minimal FastAPI app — just enough to prove the rep-session system
works end-to-end over real HTTP, not just as isolated function calls.
The actual rep-facing dashboard (prioritized queue, approve/reject,
diff view, etc.) is Step 3 — this only covers Step 1's authentication
piece: login, logout, and a protected endpoint demonstrating the
AUTHENTICATION GUARD from PRD v1.04's system prompt.
"""

from collections.abc import Generator

from fastapi import Cookie, Depends, FastAPI, HTTPException, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from leadpilot import auth, google_credentials, google_oauth
from leadpilot.config import settings
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
GOOGLE_OAUTH_CODE_VERIFIER_COOKIE = "leadpilot_google_oauth_code_verifier"


@app.get("/auth/google/connect")
def google_connect(response: Response, rep: Rep = Depends(require_rep)):
    """Step 1 of the OAuth round trip. Requires an already-logged-in
    rep (Decision 013's session, not Google's) — connecting a Google
    account is something an authenticated rep does, not a login method
    itself (see Decision 023: rep login stays email+password).
    """
    state = google_oauth.generate_state()
    auth_url, signed_code_verifier = google_oauth.build_authorization_url(state)
    redirect = RedirectResponse(url=auth_url)
    redirect.set_cookie(
        key=GOOGLE_OAUTH_STATE_COOKIE,
        value=state,
        httponly=True,
        samesite="lax",
        max_age=google_oauth.STATE_MAX_AGE_SECONDS,
    )
    # PKCE — google-auth-oauthlib generates a code_verifier per Flow
    # instance and needs it back at token-exchange time; /callback is a
    # separate request (likely a separate Flow object entirely), so it
    # has to be carried across the same way `state` already is.
    redirect.set_cookie(
        key=GOOGLE_OAUTH_CODE_VERIFIER_COOKIE,
        value=signed_code_verifier,
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
    leadpilot_google_oauth_code_verifier: str | None = Cookie(default=None),
    rep: Rep = Depends(require_rep),
    db: Session = Depends(get_db),
):
    if not google_oauth.verify_state(leadpilot_google_oauth_state, state):
        raise HTTPException(status_code=400, detail="Invalid or expired OAuth state — try connecting again")
    response.delete_cookie(GOOGLE_OAUTH_STATE_COOKIE)

    if not leadpilot_google_oauth_code_verifier:
        raise HTTPException(status_code=400, detail="Missing PKCE code verifier — try connecting again")
    response.delete_cookie(GOOGLE_OAUTH_CODE_VERIFIER_COOKIE)

    try:
        refresh_token = google_oauth.exchange_code_for_refresh_token(code, leadpilot_google_oauth_code_verifier)
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


@app.get("/dev/picker-test", response_class=HTMLResponse)
def picker_test_harness():
    """NOT Step 3's real UI — a bare page so Step 2 tools (fetch_all_leads
    etc.) can be tested against real Picker-granted access, since
    Google's drive.file scope only actually grants access to a file
    once it's been selected through the real Picker widget (or created
    by the app) — there's no way to fake that server-side. Gated behind
    ENVIRONMENT so it's never reachable outside local dev.
    """
    if settings.environment != "development":
        raise HTTPException(status_code=404)

    # Picker's setAppId — the numeric Cloud project number, which is
    # the segment before the first "-" in a Google OAuth client ID
    # (e.g. "56917149985" in "56917149985-abc...apps.googleusercontent.com").
    # Required specifically for drive.file scope: without it, Picker
    # still shows files and fires a real "picked" callback, but Google
    # never actually registers the per-file grant server-side against
    # this OAuth client — the selection looks like it worked, but the
    # access token still can't read the file afterward. This was a
    # real bug caught live, not a hypothetical.
    app_id = settings.google_oauth_client_id.split("-")[0]

    return f"""<!DOCTYPE html>
<html>
<head><title>LeadPilot — Picker test harness (dev only)</title></head>
<body>
<h1>Google Picker test harness</h1>
<p>Not the real Step 3 UI — exists so Step 2 tools can be tested against real Picker-granted files.</p>
<p>Log in first via <a href="/docs">/docs</a> (POST /login), then come back to this page.</p>
<button id="connect">1. Connect Google Account</button>
<button id="pick" disabled>2. Pick a sheet</button>
<button id="pick-folder" disabled>3. Pick a Drive folder (for verify_drive_contents)</button>
<pre id="log" style="white-space: pre-wrap; background: #eee; padding: 1em;"></pre>
<script src="https://apis.google.com/js/api.js"></script>
<script>
  const log = (msg) => {{ document.getElementById('log').textContent += msg + '\\n'; }};

  document.getElementById('connect').onclick = () => {{
    window.location.href = '/auth/google/connect';
  }};

  gapi.load('picker', () => {{
    document.getElementById('pick').disabled = false;
    document.getElementById('pick-folder').disabled = false;
    log('Picker API loaded.');
  }});

  const grantPicked = async (data) => {{
    if (data.action === google.picker.Action.PICKED) {{
      const fileId = data.docs[0].id;
      log('Picked: ' + data.docs[0].name + ' (' + fileId + ')');
      const grantResp = await fetch('/auth/google/grant-file', {{
        method: 'POST',
        headers: {{ 'Content-Type': 'application/json' }},
        body: JSON.stringify({{ file_id: fileId }}),
      }});
      const result = await grantResp.json();
      log('Granted. This rep can now access: ' + JSON.stringify(result.granted_file_ids));
    }}
  }};

  document.getElementById('pick').onclick = async () => {{
    const resp = await fetch('/auth/google/access-token');
    if (!resp.ok) {{
      log('Not connected yet (HTTP ' + resp.status + ') — click "Connect Google Account" first.');
      return;
    }}
    const {{ access_token }} = await resp.json();
    log('Got access token, opening Picker...');

    const picker = new google.picker.PickerBuilder()
      .addView(google.picker.ViewId.SPREADSHEETS)
      .setOAuthToken(access_token)
      .setDeveloperKey('{settings.google_picker_api_key}')
      .setAppId('{app_id}')
      .setCallback(grantPicked)
      .build();
    picker.setVisible(true);
  }};

  document.getElementById('pick-folder').onclick = async () => {{
    const resp = await fetch('/auth/google/access-token');
    if (!resp.ok) {{
      log('Not connected yet (HTTP ' + resp.status + ') — click "Connect Google Account" first.');
      return;
    }}
    const {{ access_token }} = await resp.json();
    log('Got access token, opening Picker (folder mode)...');

    // setSelectFolderEnabled makes the folder itself the pickable item
    // (default FOLDERS view only lets you navigate into folders, not
    // select one) — needed so verify_drive_contents has a folder_id
    // it's actually been granted, not just a sheet_id.
    const folderView = new google.picker.DocsView(google.picker.ViewId.FOLDERS)
      .setSelectFolderEnabled(true);

    const picker = new google.picker.PickerBuilder()
      .addView(folderView)
      .setOAuthToken(access_token)
      .setDeveloperKey('{settings.google_picker_api_key}')
      .setAppId('{app_id}')
      .setCallback(grantPicked)
      .build();
    picker.setVisible(true);
  }};
</script>
</body>
</html>"""


class GrantFileRequest(BaseModel):
    file_id: str


@app.post("/auth/google/grant-file")
def google_grant_file(payload: GrantFileRequest, rep: Rep = Depends(require_rep), db: Session = Depends(get_db)):
    """The other half of the Picker flow (Step 3 calls access-token to
    open it, then calls this once per file the rep selects to actually
    persist the grant) — read-only until this is called; picking a
    file in the widget alone doesn't grant anything server-side.
    Requires the rep to already be connected (a file grant with no
    underlying OAuth connection makes no sense) — same 404 as
    access-token for a rep who hasn't connected yet.
    """
    if google_credentials.get_refresh_token(db, rep.rep_id) is None:
        raise HTTPException(status_code=404, detail="Rep has not connected a Google account")
    google_credentials.add_granted_file(db, rep.rep_id, payload.file_id)
    db.commit()
    return {"granted_file_ids": google_credentials.granted_file_ids(db, rep.rep_id)}
