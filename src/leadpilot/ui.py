"""Step 3 — the rep-facing workspace (design spec v001, interface
context v002). Server-rendered Jinja2 + htmx partial swaps; every
route that touches lead/contact data depends on the same
AUTHENTICATION GUARD as the JSON API (require_rep_ui below wraps
auth.get_rep_for_signed_token), and every side-effect goes through
gate.py + the tools' own execute functions — this module never calls
a provider API directly and never invents a second approval mechanism.

Approve = gate.approve() + the tool's execute in one rep click. The
spec's "Approve-and-verb" buttons are one action to the rep; the state
machine underneath is unchanged (drafted → awaiting_rep_approval →
approved → executed), it just passes through `approved` within a
single request.

Failure policy on execute: if the tool raised *after* winning
gate.try_execute(), the approval is consumed — this module commits the
EXECUTED flip and shows the failure on the card rather than rolling
back to APPROVED. A retry that could double-send is strictly worse
than a lost send for this product (0% duplicate contact rate is the
named success metric); a genuinely unsent action gets re-staged
through a fresh draft, never replayed. If the tool raised *before*
try_execute (config/validation errors), nothing was consumed and the
card keeps its approve button.

External clients (Twilio/Gmail/Slack/Sheets/Drive) are built by the
tools themselves when the factories below return None — tests
monkeypatch the factories to inject fakes, same DI pattern as the
tools' own tests.
"""

import json
import logging
import uuid
from pathlib import Path

from fastapi import APIRouter, Cookie, Depends, Form, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from leadpilot import auth, gate, google_credentials, queue_builder
from leadpilot.config import settings
from leadpilot.connectors.base import ConcurrentWriteError, StaleWriteError
from leadpilot.connectors.google_sheets import RepNotConnectedError
from leadpilot.models.contact_history import (
    Channel,
    ContactHistory,
    Outcome,
    Stage,
    Tool,
)
from leadpilot.models.leads import LEAD_STATUS_OPTIONS, Lead
from leadpilot.models.rep import Rep
from leadpilot.tools import (
    dispatch_slack_handoff,
    fetch_ad_hoc_sheet,
    fetch_all_leads,
    initiate_lead_call,
    log_call_outcome,
    search_communications as search_comms_module,
    send_lead_email,
    send_lead_text,
    update_lead_sheet,
    verify_drive_contents,
)
from leadpilot.tools.fetch_all_leads import RunAlreadyInProgressError

logger = logging.getLogger("leadpilot.auth_guard")

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

SESSION_COOKIE_NAME = "leadpilot_session"


# ---- Injectable client factories (tests monkeypatch these) -----------
# Returning None means "let the tool build its real client."

def twilio_client_factory():
    return None


def gmail_service_factory():
    return None


def slack_client_factory():
    return None


def sheets_connector_factory(db: Session, rep_id: uuid.UUID):
    return None


def drive_client_factory(db: Session, rep_id: uuid.UUID):
    return None


# ---- Auth: UI variant of the AUTHENTICATION GUARD --------------------


class LoginRequiredError(Exception):
    """Raised instead of app.py's 401 JSON — pages redirect to /login,
    htmx partials get an HX-Redirect so the whole window navigates
    rather than swapping a login page into a pane.
    """


def login_required_handler(request: Request, exc: LoginRequiredError):
    if request.headers.get("HX-Request"):
        return Response(status_code=401, headers={"HX-Redirect": "/login"})
    return RedirectResponse("/login", status_code=303)


def get_db_ui(request: Request):
    from leadpilot.app import get_db  # late import — app.py imports this module

    yield from get_db()


def require_rep_ui(
    leadpilot_session: str | None = Cookie(default=None),
    db: Session = Depends(get_db_ui),
) -> Rep:
    """testing/eval-suite.md Case 6: an unauthenticated request must be
    logged, not just rejected — see app.py's require_rep for the same
    fix on the JSON API side.
    """
    if leadpilot_session is None:
        logger.warning("Rejected unauthenticated UI request: no session cookie present")
        raise LoginRequiredError()
    rep = auth.get_rep_for_signed_token(db, leadpilot_session)
    if rep is None:
        logger.warning("Rejected unauthenticated UI request: session cookie present but invalid/expired")
        raise LoginRequiredError()
    return rep


# ---- Login / logout (§6a) ---------------------------------------------


@router.get("/login", response_class=HTMLResponse)
def login_page(
    request: Request,
    leadpilot_session: str | None = Cookie(default=None),
    db: Session = Depends(get_db_ui),
):
    if leadpilot_session and auth.get_rep_for_signed_token(db, leadpilot_session):
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse(request, "login.html", {"error": None})


@router.post("/login/form")
def login_form(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    db: Session = Depends(get_db_ui),
):
    rep = auth.authenticate(db, email=email, password=password)
    if rep is None:
        db.commit()  # persist the failed-attempt log
        return templates.TemplateResponse(
            request, "login.html", {"error": "Invalid email or password."}, status_code=401
        )
    signed_token = auth.create_session(db, rep.rep_id)
    db.commit()
    response = RedirectResponse("/", status_code=303)
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=signed_token,
        httponly=True,
        samesite="lax",
        max_age=int(auth.DEFAULT_SESSION_TTL.total_seconds()),
    )
    return response


@router.post("/logout/form")
def logout_form(
    leadpilot_session: str | None = Cookie(default=None),
    db: Session = Depends(get_db_ui),
):
    if leadpilot_session is not None:
        auth.revoke_session(db, leadpilot_session)
        db.commit()
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(SESSION_COOKIE_NAME)
    return response


# ---- Workspace shell + queue (§4) --------------------------------------


@router.get("/", response_class=HTMLResponse)
def workspace(request: Request, rep: Rep = Depends(require_rep_ui)):
    return templates.TemplateResponse(request, "workspace.html", {"rep": rep})


def _queue_context(db: Session, rep: Rep, q: str | None = None, **extra) -> dict:
    return {
        "queue": queue_builder.build_queue(db, rep.rep_id, q=q),
        "unlogged": queue_builder.unlogged_calls(db, rep.rep_id),
        "selected_lead_id": None,
        "sync_message": None,
        "sync_error": False,
        "q": q,
        **extra,
    }


@router.get("/ui/queue", response_class=HTMLResponse)
def queue_partial(
    request: Request,
    q: str | None = Query(default=None),
    list_only: bool = Query(default=False),
    rep: Rep = Depends(require_rep_ui),
    db: Session = Depends(get_db_ui),
):
    template = "partials/queue_list.html" if list_only else "partials/queue.html"
    return templates.TemplateResponse(request, template, _queue_context(db, rep, q=q))


@router.post("/ui/sync", response_class=HTMLResponse)
def sync_sheets(
    request: Request,
    rep: Rep = Depends(require_rep_ui),
    db: Session = Depends(get_db_ui),
):
    """The same scan the hourly run performs, on demand — the real
    fetch_all_leads tool, run lock and all.
    """
    message, is_error = None, False
    try:
        rows = fetch_all_leads.run(
            db, rep.rep_id, connector=sheets_connector_factory(db, rep.rep_id)
        )
        if rows:
            message = f"Synced {len(rows)} rows from your granted sheets."
        else:
            message = "Sync ran, but no granted sheets had rows — grant sheets via Connect Google."
    except RunAlreadyInProgressError:
        message, is_error = "A sync is already running for you — try again shortly.", True
    except RepNotConnectedError:
        message, is_error = "Connect your Google account first (Connect Google, top right).", True
    except Exception as e:  # surfaced, not swallowed — reps route around silent failure
        message, is_error = f"Sync failed: {e}", True

    return templates.TemplateResponse(
        request,
        "partials/queue.html",
        _queue_context(db, rep, sync_message=message, sync_error=is_error),
    )


# ---- Lead center pane + context rail -----------------------------------


def _rep_names(db: Session) -> dict:
    return {r.rep_id: (r.display_name or r.email) for r in db.execute(select(Rep)).scalars()}


def _lead_or_404(db: Session, lead_id: uuid.UUID) -> Lead:
    from fastapi import HTTPException

    lead = db.get(Lead, lead_id)
    if lead is None:
        raise HTTPException(status_code=404, detail=f"No such lead {lead_id}")
    return lead


def _center_context(db: Session, rep: Rep, lead: Lead, **extra) -> dict:
    from datetime import datetime, timezone

    events = (
        db.execute(select(ContactHistory).where(ContactHistory.lead_id == lead.lead_id))
        .scalars()
        .all()
    )
    rank, reason = queue_builder._rank(events, datetime.now(timezone.utc))
    names = _rep_names(db)

    # Pipeline status from the sheet's Status column (first source row
    # with a mapped value) — shown as a chip in the lead header.
    from leadpilot.connectors.google_sheets import _map_row_fields

    lead_status = None
    for src in queue_builder.lead_sources(db, lead.lead_id):
        mapped = _map_row_fields(src.raw_data or {})
        if mapped.get("status"):
            lead_status = mapped["status"]
            break
    cards = [
        queue_builder.describe_event(e, names, rep.rep_id)
        for e in queue_builder.pending_actions(db, lead.lead_id)
    ]
    return {
        "lead": lead,
        "rank": rank,
        "rank_reason": reason,
        "flagged": queue_builder._lead_is_flagged(lead),
        "cards": cards,
        "sources": queue_builder.lead_sources(db, lead.lead_id),
        "edit": False,
        "result": None,
        "stage_error": None,
        "status_options": LEAD_STATUS_OPTIONS,
        "lead_status": lead_status,
        # rail context, rendered via the same response's OOB swap
        "events": queue_builder.timeline(db, lead.lead_id, rep.rep_id),
        "docs": None,
        "docs_error": None,
        "folder_options": [i for i in granted_items(db, rep.rep_id) if i["is_folder"]],
        "folder_id": None,
        **extra,
    }


@router.get("/ui/leads/{lead_id}", response_class=HTMLResponse)
def lead_center(
    request: Request,
    lead_id: uuid.UUID,
    rep: Rep = Depends(require_rep_ui),
    db: Session = Depends(get_db_ui),
):
    lead = _lead_or_404(db, lead_id)
    return templates.TemplateResponse(
        request, "partials/lead_center.html", _center_context(db, rep, lead)
    )


@router.get("/ui/leads/{lead_id}/rail", response_class=HTMLResponse)
def lead_rail(
    request: Request,
    lead_id: uuid.UUID,
    folder_id: str | None = Query(default=None),
    rep: Rep = Depends(require_rep_ui),
    db: Session = Depends(get_db_ui),
):
    """Standalone rail refresh — used by the folder <select> to run a
    real verify_drive_contents check against a granted folder.
    """
    lead = _lead_or_404(db, lead_id)
    docs, docs_error = None, None
    if folder_id:
        try:
            files = verify_drive_contents.run(
                db, rep.rep_id, folder_id, client=drive_client_factory(db, rep.rep_id)
            )
            docs = queue_builder.doc_checklist(files)
        except Exception as e:
            docs_error = f"Couldn't read that folder: {e}"
    ctx = _center_context(db, rep, lead, docs=docs, docs_error=docs_error, folder_id=folder_id)
    return templates.TemplateResponse(request, "partials/rail.html", ctx)


# ---- Approve / reject / edit (the actual gate wiring) -------------------


def _get_event(db: Session, event_id: uuid.UUID) -> ContactHistory | None:
    return db.get(ContactHistory, event_id)


def _card_response(
    request: Request, db: Session, rep: Rep, event: ContactHistory,
    edit: bool = False, result: dict | None = None, status_code: int = 200,
):
    c = queue_builder.describe_event(event, _rep_names(db), rep.rep_id)
    return templates.TemplateResponse(
        request,
        "partials/action_card.html",
        {"c": c, "edit": edit, "result": result},
        status_code=status_code,
    )


@router.get("/ui/actions/{event_id}/card", response_class=HTMLResponse)
def action_card(
    request: Request,
    event_id: uuid.UUID,
    edit: bool = Query(default=False),
    rep: Rep = Depends(require_rep_ui),
    db: Session = Depends(get_db_ui),
):
    event = _get_event(db, event_id)
    if event is None:
        return HTMLResponse("<div class='notice-error'>This action no longer exists.</div>")
    return _card_response(request, db, rep, event, edit=edit)


@router.post("/ui/actions/{event_id}/edit", response_class=HTMLResponse)
def edit_draft(
    request: Request,
    event_id: uuid.UUID,
    body: str = Form(...),
    subject: str | None = Form(default=None),
    rep: Rep = Depends(require_rep_ui),
    db: Session = Depends(get_db_ui),
):
    """Edit a still-pending draft's content. Same atomic-conditional-
    UPDATE discipline as gate.py: only lands if the row is still
    awaiting approval, so an edit can never rewrite something already
    approved/executed/rejected.
    """
    event = _get_event(db, event_id)
    if event is None:
        return HTMLResponse("<div class='notice-error'>This action no longer exists.</div>")

    if event.tool == Tool.SEND_LEAD_EMAIL:
        existing = json.loads(event.content_ref)
        new_content = json.dumps(
            {"subject": subject or existing["subject"], "body": body, "to": existing["to"]}
        )
    elif event.tool in (Tool.SEND_LEAD_TEXT, Tool.DISPATCH_SLACK_HANDOFF):
        new_content = body
    else:
        return _card_response(
            request, db, rep, event,
            result={"error_message": "This draft type isn't editable — reject it and stage a fresh one."},
        )

    updated = db.execute(
        update(ContactHistory)
        .where(
            ContactHistory.event_id == event_id,
            ContactHistory.stage == Stage.AWAITING_REP_APPROVAL,
        )
        .values(content_ref=new_content)
    )
    db.commit()
    result = None
    if updated.rowcount != 1:
        result = {"error_message": "This draft was already handled — edit didn't apply."}
    db.expire_all()
    event = _get_event(db, event_id)
    return _card_response(request, db, rep, event, result=result)


@router.post("/ui/actions/{event_id}/reject", response_class=HTMLResponse)
def reject_action(
    request: Request,
    event_id: uuid.UUID,
    rep: Rep = Depends(require_rep_ui),
    db: Session = Depends(get_db_ui),
):
    event = _get_event(db, event_id)
    if event is None:
        return HTMLResponse("<div class='notice-error'>This action no longer exists.</div>")
    gate.reject(db, event_id, rep_id=rep.rep_id)
    db.commit()
    db.expire_all()
    event = _get_event(db, event_id)
    return _card_response(request, db, rep, event)


@router.post("/ui/actions/{event_id}/approve", response_class=HTMLResponse)
def approve_action(
    request: Request,
    event_id: uuid.UUID,
    rep: Rep = Depends(require_rep_ui),
    db: Session = Depends(get_db_ui),
):
    """The click that makes a draft real: gate.approve() then the
    tool's own execute (which re-checks gate.try_execute itself —
    Decision 021's single-use guarantee holds even if two approve
    requests race).
    """
    event = _get_event(db, event_id)
    if event is None:
        return HTMLResponse("<div class='notice-error'>This action no longer exists.</div>")

    approved = gate.approve(db, event_id, rep_id=rep.rep_id)
    db.commit()
    db.expire_all()
    event = _get_event(db, event_id)
    if not approved and event.stage != Stage.APPROVED:
        # Already executed/rejected/expired — render the true state.
        return _card_response(
            request, db, rep, event,
            result={"error_message": "This action was already handled."},
        )

    result: dict = {}
    try:
        if event.tool == Tool.INITIATE_LEAD_CALL:
            phone = initiate_lead_call.execute_initiate_lead_call(db, event_id=event_id)
            db.commit()
            if phone:
                result["copy_phone"] = phone
        elif event.tool == Tool.SEND_LEAD_TEXT:
            outcome = send_lead_text.execute_send_lead_text(
                db, event_id=event_id, twilio_client=twilio_client_factory()
            )
            _set_outcome(db, event_id, Outcome.DELIVERED if outcome else None)
            db.commit()
            if outcome:
                result["ok_message"] = f"Text sent to {outcome['to']}."
        elif event.tool == Tool.SEND_LEAD_EMAIL:
            outcome = send_lead_email.execute_send_lead_email(
                db, event_id=event_id, gmail_service=gmail_service_factory()
            )
            _set_outcome(db, event_id, Outcome.DELIVERED if outcome else None)
            db.commit()
            if outcome:
                result["ok_message"] = f"Email sent to {outcome['to']} from your Gmail."
        elif event.tool == Tool.DISPATCH_SLACK_HANDOFF:
            outcome = dispatch_slack_handoff.execute_dispatch_slack_handoff(
                db, event_id=event_id, slack_client=slack_client_factory()
            )
            _set_outcome(db, event_id, Outcome.DELIVERED if outcome else None)
            db.commit()
            if outcome:
                ok = sum(1 for d in outcome["deliveries"] if d["ok"])
                result["ok_message"] = f"Posted to {ok} of {len(outcome['deliveries'])} stakeholder channels."
        elif event.tool == Tool.UPDATE_LEAD_SHEET:
            outcome = update_lead_sheet.execute(
                db, event_id, connector=sheets_connector_factory(db, event.rep_id or rep.rep_id)
            )
            if not outcome.get("executed"):
                result["error_message"] = "This write was already handled."

    except StaleWriteError as stale:
        # §6e: the sanctioned prominent conflict panel, not a generic
        # error. The approval was consumed (row is EXECUTED, write
        # held) — recovery is a FRESH staged diff, never a replay.
        db.commit()
        info = json.loads(event.content_ref)
        return templates.TemplateResponse(
            request,
            "partials/stale_conflict.html",
            {
                "event_id": str(event_id),
                "lead_id": str(event.lead_id),
                "conflict": {
                    "source_id": stale.source_id,
                    "row_ref": stale.row_ref,
                    "field": stale.field,
                    "expected": stale.expected,
                    "actual": stale.actual,
                    "proposed": info["value"],
                },
            },
        )
    except ConcurrentWriteError:
        db.commit()
        result["error_message"] = (
            "Another write to this exact cell is in flight — the approval was "
            "consumed without writing. Stage a fresh edit to retry."
        )
    except Exception as e:
        # Consumed-after-flip failures keep the EXECUTED flip (see
        # module docstring's failure policy); pre-flip validation
        # failures left the row APPROVED and the button available.
        db.commit()
        _set_outcome(db, event_id, Outcome.FAILED, only_if_executed=True)
        db.commit()
        result["error_message"] = f"Execution failed: {e}"

    db.expire_all()
    event = _get_event(db, event_id)
    return _card_response(request, db, rep, event, result=result or None)


def _set_outcome(
    db: Session, event_id: uuid.UUID, outcome: Outcome | None, only_if_executed: bool = False
) -> None:
    """Provider-reported delivery outcome onto the row (the Outcome
    enum's documented use for texts/emails/Slack). The tools return
    delivery results but don't persist them — the approving caller
    (here) is the integration point that does.
    """
    if outcome is None:
        return
    stmt = update(ContactHistory).where(ContactHistory.event_id == event_id)
    if only_if_executed:
        stmt = stmt.where(
            ContactHistory.stage == Stage.EXECUTED,
            ContactHistory.outcome.is_(None),
        )
    db.execute(stmt.values(outcome=outcome))


# ---- Call outcomes (§6f) ------------------------------------------------


@router.post("/ui/calls/{event_id}/outcome", response_class=HTMLResponse)
def call_outcome(
    request: Request,
    event_id: uuid.UUID,
    outcome: str = Form(...),
    note: str | None = Form(default=None),
    rep: Rep = Depends(require_rep_ui),
    db: Session = Depends(get_db_ui),
):
    error = None
    try:
        log_call_outcome.run(db, event_id, outcome, note=note)
    except ValueError as e:
        error = str(e)

    # Same endpoint serves both moments (§6f): the inline card row and
    # the queue's amber strip — htmx tells us which via HX-Target.
    if request.headers.get("HX-Target") == "queue-pane":
        ctx = _queue_context(db, rep)
        if error:
            ctx.update(sync_message=error, sync_error=True)
        return templates.TemplateResponse(request, "partials/queue.html", ctx)

    event = _get_event(db, event_id)
    result = {"error_message": error} if error else {"ok_message": "Outcome logged."}
    return _card_response(request, db, rep, event, result=result)


# ---- Rep-initiated action drafts (Marc, 2026-07-15) ----------------------
# The agent isn't the only one who drafts: reps can stage a call/text/
# email directly from the lead view. Same path through the gate — the
# tools stage AWAITING_REP_APPROVAL, the card appears, approval fires
# it. No shortcut around the state machine, just a human author.


@router.post("/ui/leads/{lead_id}/stage-call", response_class=HTMLResponse)
def stage_call(
    request: Request,
    lead_id: uuid.UUID,
    rep: Rep = Depends(require_rep_ui),
    db: Session = Depends(get_db_ui),
):
    lead = _lead_or_404(db, lead_id)
    stage_error = None
    try:
        initiate_lead_call.initiate_lead_call(db, lead_id=lead_id)
        db.commit()
    except ValueError as e:
        db.rollback()
        stage_error = str(e)
    return templates.TemplateResponse(
        request, "partials/lead_center.html", _center_context(db, rep, lead, stage_error=stage_error)
    )


@router.post("/ui/leads/{lead_id}/stage-text", response_class=HTMLResponse)
def stage_text(
    request: Request,
    lead_id: uuid.UUID,
    message: str = Form(...),
    rep: Rep = Depends(require_rep_ui),
    db: Session = Depends(get_db_ui),
):
    lead = _lead_or_404(db, lead_id)
    stage_error = None
    try:
        send_lead_text.send_lead_text(db, lead_id=lead_id, message=message)
        db.commit()
    except ValueError as e:
        db.rollback()
        stage_error = str(e)
    return templates.TemplateResponse(
        request, "partials/lead_center.html", _center_context(db, rep, lead, stage_error=stage_error)
    )


@router.post("/ui/leads/{lead_id}/stage-email", response_class=HTMLResponse)
def stage_email(
    request: Request,
    lead_id: uuid.UUID,
    subject: str = Form(...),
    body: str = Form(...),
    rep: Rep = Depends(require_rep_ui),
    db: Session = Depends(get_db_ui),
):
    lead = _lead_or_404(db, lead_id)
    stage_error = None
    try:
        send_lead_email.send_lead_email(db, lead_id=lead_id, subject=subject, body=body)
        db.commit()
    except ValueError as e:
        db.rollback()
        stage_error = str(e)
    return templates.TemplateResponse(
        request, "partials/lead_center.html", _center_context(db, rep, lead, stage_error=stage_error)
    )


# ---- Sheet edits: staging + tabs (§6e / §4) ------------------------------


@router.post("/ui/leads/{lead_id}/stage-edit", response_class=HTMLResponse)
def stage_sheet_edit(
    request: Request,
    lead_id: uuid.UUID,
    source_row: str = Form(...),
    field: str = Form(...),
    value: str = Form(...),
    rep: Rep = Depends(require_rep_ui),
    db: Session = Depends(get_db_ui),
):
    """Stages a real update_lead_sheet diff (fresh read of the live
    cell) — used by both the inline edit form and the stale-write
    panel's "apply my edit anyway" exit.
    """
    lead = _lead_or_404(db, lead_id)
    source_id, _, row_ref = source_row.partition("|")
    stage_error = None
    try:
        update_lead_sheet.run(
            db, rep.rep_id, lead_id, source_id, row_ref, field, value,
            connector=sheets_connector_factory(db, rep.rep_id),
        )
    except Exception as e:
        stage_error = f"Couldn't stage this edit: {e}"

    return templates.TemplateResponse(
        request, "partials/lead_center.html",
        _center_context(db, rep, lead, stage_error=stage_error),
    )


@router.get("/ui/sheet-tools", response_class=HTMLResponse)
def sheet_tools(
    request: Request,
    rep: Rep = Depends(require_rep_ui),
    db: Session = Depends(get_db_ui),
):
    items = granted_items(db, rep.rep_id)
    return templates.TemplateResponse(
        request,
        "partials/sheet_tools.html",
        {"granted": [i for i in items if not i["is_folder"]]},
    )


@router.get("/ui/docs-tools", response_class=HTMLResponse)
def docs_tools(
    request: Request,
    folder_id: str | None = Query(default=None),
    rep: Rep = Depends(require_rep_ui),
    db: Session = Depends(get_db_ui),
):
    docs, files, docs_error = None, None, None
    if folder_id:
        try:
            files = verify_drive_contents.run(
                db, rep.rep_id, folder_id, client=drive_client_factory(db, rep.rep_id)
            )
            docs = queue_builder.doc_checklist(files)
        except Exception as e:
            docs_error = f"Couldn't read that folder: {e}"
    return templates.TemplateResponse(
        request,
        "partials/docs_tools.html",
        {
            "folders": [i for i in granted_items(db, rep.rep_id) if i["is_folder"]],
            "folder_id": folder_id,
            "docs": docs,
            "files": files,
            "docs_error": docs_error,
        },
    )


@router.post("/ui/adhoc", response_class=HTMLResponse)
def adhoc_sheet(
    request: Request,
    source_id: str = Form(...),
    rep: Rep = Depends(require_rep_ui),
    db: Session = Depends(get_db_ui),
):
    before = {row for row in db.execute(select(Lead.lead_id)).scalars()}
    connector = sheets_connector_factory(db, rep.rep_id)
    try:
        rows = fetch_ad_hoc_sheet.run(db, rep.rep_id, source_id, connector=connector)
    except Exception as e:
        return templates.TemplateResponse(request, "partials/adhoc_result.html", {"error": str(e)})
    new_count = sum(1 for r in rows if uuid.UUID(r["lead_id"]) not in before)
    flagged_count = sum(1 for r in rows if r["flagged"])

    # Status-column detection (Marc, 2026-07-15): real intake sheets
    # often lack one, and without it update_lead_sheet has nowhere to
    # record pipeline stage. Detection failure never hides the read
    # result — worst case the prompt just doesn't show.
    missing_status = False
    try:
        checker = connector or _real_sheets_connector(db, rep.rep_id)
        if hasattr(checker, "has_field_column"):
            missing_status = not checker.has_field_column(source_id, "status")
    except Exception:
        pass

    return templates.TemplateResponse(
        request,
        "partials/adhoc_result.html",
        {
            "error": None, "count": len(rows), "new_count": new_count,
            "flagged_count": flagged_count, "missing_status": missing_status,
            "source_id": source_id,
        },
        # Tell the workspace queue to refresh — the read happens in a
        # drawer/tab, so the queue pane can't see it otherwise.
        headers={"HX-Trigger": "leads-changed"},
    )


def _real_sheets_connector(db: Session, rep_id: uuid.UUID):
    from leadpilot.connectors.google_sheets import GoogleSheetsConnector

    return GoogleSheetsConnector(db, rep_id)


# file_id -> {"name", "mime"} — process-lifetime cache. Titles rarely
# change and the rail re-renders on every lead click; without this,
# each click would cost one Drive metadata call per granted item.
_file_info_cache: dict[str, dict] = {}

_SPREADSHEET_MIME = "application/vnd.google-apps.spreadsheet"
_FOLDER_MIME = "application/vnd.google-apps.folder"


def granted_items(db: Session, rep_id: uuid.UUID) -> list[dict]:
    """The rep's granted ids resolved to display names + kind, for
    every dropdown/list that previously showed raw ids (Marc,
    2026-07-15). A failed lookup degrades to the id as the name —
    never blocks rendering.
    """
    items = []
    drive = drive_client_factory(db, rep_id)
    for file_id in google_credentials.granted_file_ids(db, rep_id):
        info = _file_info_cache.get(file_id)
        if info is None:
            try:
                client = drive or _real_drive_client(db, rep_id)
                meta = client.file_info(file_id)
                info = {"name": meta.get("name") or file_id, "mime": meta.get("mimeType") or ""}
            except Exception:
                info = {"name": file_id, "mime": ""}
            _file_info_cache[file_id] = info
        items.append(
            {
                "id": file_id,
                "name": info["name"],
                "is_sheet": info["mime"] == _SPREADSHEET_MIME,
                "is_folder": info["mime"] == _FOLDER_MIME,
            }
        )
    return items


def _real_drive_client(db: Session, rep_id: uuid.UUID):
    from leadpilot.connectors.google_drive import GoogleDriveClient

    return GoogleDriveClient(db, rep_id)


@router.post("/ui/sheets/create-status-column", response_class=HTMLResponse)
def create_status_column(
    request: Request,
    source_id: str = Form(...),
    rep: Rep = Depends(require_rep_ui),
    db: Session = Depends(get_db_ui),
):
    """Rep-confirmed creation of a Status column in a granted sheet.
    The confirm click in the prompt IS the rep approval (rep-initiated
    sheet structure, no lead attached — same precedent as
    log_call_outcome); the connector call is idempotent.
    """
    connector = sheets_connector_factory(db, rep.rep_id) or _real_sheets_connector(db, rep.rep_id)
    try:
        header = connector.add_status_column(source_id)
        return HTMLResponse(
            f'<div class="card-status ok" style="font-size: 12.5px;">'
            f"Created a “{header}” column in the sheet. New pipeline stages "
            f"written from LeadPilot will land there.</div>"
        )
    except Exception as e:
        return HTMLResponse(f'<div class="notice-error">Couldn&#39;t create the column: {e}</div>')


# ---- Search (§6h) --------------------------------------------------------


@router.get("/ui/search", response_class=HTMLResponse)
def search_drawer(request: Request, rep: Rep = Depends(require_rep_ui)):
    return templates.TemplateResponse(request, "partials/search_drawer.html", {})


@router.get("/ui/search/results", response_class=HTMLResponse)
def search_results(
    request: Request,
    q: str = Query(...),
    rep: Rep = Depends(require_rep_ui),
    db: Session = Depends(get_db_ui),
):
    q = q.strip()
    is_phone = search_comms_module._looks_like_phone(q)
    name_search = not is_phone and "@" not in q

    # Identity header: does this identifier match a lead we know?
    lead = None
    if q:
        stmt = select(Lead).where(
            (Lead.primary_phone == q)
            | (Lead.primary_email == q.lower())
            | Lead.display_name.ilike(f"%{q}%")
            | Lead.company.ilike(f"%{q}%")
        )
        lead = db.execute(stmt).scalars().first()

    results, error = None, None
    try:
        results = search_comms_module.search_communications(
            db,
            rep_id=rep.rep_id,
            identifier=q,
            gmail_service=gmail_service_factory(),
            twilio_client=twilio_client_factory(),
        )
    except RepNotConnectedError:
        error = "Connect your Google account to search email history (Connect Google, top right)."
    except ValueError as e:
        error = str(e)
    except Exception as e:
        error = f"Search failed: {e}"

    return templates.TemplateResponse(
        request,
        "partials/search_results.html",
        {"lead": lead, "results": results, "error": error, "name_search": name_search},
    )


# ---- Reset all lead data (rep-requested wipe, 2026-07-15) ----------------


@router.get("/ui/reset", response_class=HTMLResponse)
def reset_drawer(request: Request, rep: Rep = Depends(require_rep_ui)):
    return templates.TemplateResponse(
        request, "partials/reset_drawer.html", {"done": False, "error": None}
    )


@router.post("/ui/reset-data", response_class=HTMLResponse)
def reset_data(
    request: Request,
    confirm: str = Form(default=""),
    rep: Rep = Depends(require_rep_ui),
    db: Session = Depends(get_db_ui),
):
    """Wipes every lead and everything hanging off leads — for a clean
    re-ingest during testing/onboarding. Deliberately spares reps,
    sessions, and Google credentials/grants (reconnecting OAuth is the
    expensive part of setup). Requires the typed confirmation; a bare
    POST without it changes nothing.
    """
    if confirm.strip().upper() != "RESET":
        return templates.TemplateResponse(
            request,
            "partials/reset_drawer.html",
            {"done": False, "error": "Confirmation text didn't match — nothing was deleted."},
        )

    from leadpilot.models.agent_run_report import AgentRunReport
    from leadpilot.models.dedup import LeadSourceRow
    from leadpilot.models.run_lock import LeadActionLock, SheetCellLock

    counts = {
        "history": db.query(ContactHistory).delete(),
        "source_rows": db.query(LeadSourceRow).delete(),
        "reports": db.query(AgentRunReport).delete(),
    }
    db.query(LeadActionLock).delete()
    db.query(SheetCellLock).delete()
    counts["leads"] = db.query(Lead).delete()
    db.commit()
    logger.warning(
        "rep %s wiped all lead data: %s", rep.rep_id,
        ", ".join(f"{k}={v}" for k, v in counts.items()),
    )

    return templates.TemplateResponse(
        request,
        "partials/reset_drawer.html",
        {"done": True, "counts": counts, "error": None},
        headers={"HX-Trigger": "leads-changed"},
    )


# ---- Connect Google drawer (§6b) ----------------------------------------


@router.get("/ui/connect", response_class=HTMLResponse)
def connect_drawer(
    request: Request,
    rep: Rep = Depends(require_rep_ui),
    db: Session = Depends(get_db_ui),
):
    connected = google_credentials.get_refresh_token(db, rep.rep_id) is not None
    items = granted_items(db, rep.rep_id) if connected else []
    app_id = settings.google_oauth_client_id.split("-")[0] if settings.google_oauth_client_id else ""
    return templates.TemplateResponse(
        request,
        "partials/connect_drawer.html",
        {
            "connected": connected,
            "granted": items,
            "sheets": [i for i in items if not i["is_folder"]],
            "picker_api_key": settings.google_picker_api_key,
            "picker_app_id": app_id,
        },
    )
