"""Rep-facing email alerting for injection_guard incidents (chat
request, 2026-07-17): every sheet row the guard flags gets logged
durably (models/injection_alert.py's InjectionIncident), and — unless
the rep has turned this off, or the burst breaker is currently
open — an email about it goes to the rep's own connected Gmail
account, sent immediately with no rep-approval step.

Deliberately bypasses gate.py's drafted->approved->executed machinery:
that state machine exists to enforce "the agent must never act on a
lead without rep approval" (Decision 021). This isn't the agent acting
on a lead — it's a system security notice to the rep about their own
pipeline, so requiring the rep to approve being told about a security
event would defeat the point.

Rate limiter, exactly as specified: more than BURST_THRESHOLD
incidents in a trailing BURST_WINDOW trips a breaker — one "we're
pausing alerts" email goes out, then nothing further until an incident
arrives at least SILENCE_TO_RESUME after the previous one (checked
lazily on the next incident, never swept on a timer, same style as
run_lock.py's staleness fallback). The trailing-window count is a live
query against InjectionIncident rather than a second counter, so there
is only one source of truth for "how many incidents recently" — the
per-rep RepInjectionAlertState row just tracks the breaker's own
tripped/last-sent timestamps, updated via the same
INSERT ... ON CONFLICT convention locks.py uses for the same reason
(a single round trip, not a racy SELECT-then-UPDATE).
"""

import base64
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.mime.text import MIMEText
from enum import Enum

from google.oauth2.credentials import Credentials as GoogleCredentials
from googleapiclient.discovery import build
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from leadpilot import google_oauth
from leadpilot.models.injection_alert import InjectionIncident, RepInjectionAlertState
from leadpilot.models.rep import Rep

logger = logging.getLogger("leadpilot.injection_alerts")

BURST_THRESHOLD = 5
BURST_WINDOW = timedelta(minutes=5)
SILENCE_TO_RESUME = timedelta(hours=1)


class AlertAction(str, Enum):
    SENT_INCIDENT = "sent_incident"
    SENT_LIMIT_NOTICE = "sent_limit_notice"
    SUPPRESSED = "suppressed"
    DISABLED = "disabled"
    NOT_CONNECTED = "not_connected"


def _ensure_state_row(session: Session, rep_id: uuid.UUID) -> None:
    session.execute(
        insert(RepInjectionAlertState)
        .values(rep_id=rep_id)
        .on_conflict_do_nothing(index_elements=[RepInjectionAlertState.rep_id])
    )


def _incident_count_in_window(session: Session, rep_id: uuid.UUID, now: datetime, burst_window: timedelta) -> int:
    return session.execute(
        select(func.count()).select_from(InjectionIncident).where(
            InjectionIncident.rep_id == rep_id,
            InjectionIncident.occurred_at >= now - burst_window,
        )
    ).scalar_one()


def _compose_incident_email(source_id: str, row_ref: str, reasons: dict[str, str]) -> tuple[str, str]:
    subject = "LeadPilot security alert: suspicious data flagged in your pipeline"
    field_lines = "\n".join(f"  - {field}: {reason}" for field, reason in reasons.items())
    body = (
        "LeadPilot's injection guard flagged and neutralized suspicious data before "
        "it reached the agent.\n\n"
        f"Source sheet: {source_id}\n"
        f"Row: {row_ref}\n"
        f"Flagged field(s):\n{field_lines}\n\n"
        "The flagged field(s) were replaced with a placeholder — nothing from that "
        "row was acted on. No action is needed unless you don't recognize this sheet "
        "or want to investigate who entered this data.\n\n"
        "You can turn these email alerts off from Settings in the LeadPilot workspace."
    )
    return subject, body


def _compose_limit_email(burst_threshold: int) -> tuple[str, str]:
    subject = "LeadPilot: pausing injection-guard email alerts (too many incidents)"
    body = (
        f"More than {burst_threshold} suspicious-data incidents were flagged in your "
        "pipeline within a 5-minute span.\n\n"
        "To avoid flooding your inbox, LeadPilot is pausing per-incident email alerts "
        "for now. Every incident is still being logged and neutralized exactly as "
        "before — nothing is going unblocked, only the emails are paused.\n\n"
        "Alerts will resume automatically once a full hour passes with no further "
        "incidents. You can also check the incident log directly, or turn these "
        "alerts off entirely from Settings in the LeadPilot workspace."
    )
    return subject, body


def _send_rep_email(session: Session, rep_id: uuid.UUID, subject: str, body: str, gmail_service=None) -> bool:
    """Sends from and to the rep's own connected Gmail account — a
    self-notification, same low-level send path
    tools/send_lead_email.py's execute_send_lead_email uses, just
    without the gate.try_execute() step (see module docstring for why).
    Returns False (never raises) if the rep isn't connected — a missing
    Google connection shouldn't break the ingest pipeline that's
    calling this as a side effect of processing rows.
    """
    if gmail_service is None:
        access_token = google_oauth.get_fresh_access_token(session, rep_id)
        if access_token is None:
            logger.info("skipping injection-alert email for rep_id=%s: no connected Google account", rep_id)
            return False
        creds = GoogleCredentials(token=access_token)
        gmail_service = build("gmail", "v1", credentials=creds)

    profile = gmail_service.users().getProfile(userId="me").execute()
    to_address = profile["emailAddress"]

    message = MIMEText(body)
    message["to"] = to_address
    message["subject"] = subject
    raw = base64.urlsafe_b64encode(message.as_bytes()).decode()
    gmail_service.users().messages().send(userId="me", body={"raw": raw}).execute()
    return True


def record_incident_and_maybe_notify(
    session: Session,
    rep_id: uuid.UUID,
    source_id: str,
    row_ref: str,
    reasons: dict[str, str],
    gmail_service=None,
    burst_threshold: int = BURST_THRESHOLD,
    burst_window: timedelta = BURST_WINDOW,
    silence_to_resume: timedelta = SILENCE_TO_RESUME,
) -> AlertAction:
    """Call once per flagged sheet row (not once per flagged field —
    bundle every flagged field on that row into one incident/email, so
    a single malicious row can't burn through the burst budget alone).

    Always logs the incident first, regardless of settings or breaker
    state, so nothing is lost to "examine later."

    burst_threshold/burst_window/silence_to_resume default to the real
    module constants — overridable the same way locks.py's cooldown/
    stale_after parameters are, so tests can use trivial values (e.g.
    silence_to_resume=timedelta(seconds=0)) instead of mocking time.
    """
    now = datetime.now(timezone.utc)
    session.add(InjectionIncident(
        rep_id=rep_id, source_id=source_id, row_ref=row_ref, reasons=reasons, occurred_at=now,
    ))
    session.flush()

    rep = session.get(Rep, rep_id)
    _ensure_state_row(session, rep_id)
    state = session.execute(
        select(RepInjectionAlertState).where(RepInjectionAlertState.rep_id == rep_id).with_for_update()
    ).scalar_one()

    previous_incident_at = state.last_incident_at
    if state.tripped_at is not None and previous_incident_at is not None and (now - previous_incident_at) >= silence_to_resume:
        state.tripped_at = None

    state.last_incident_at = now

    if rep is None or not rep.injection_alerts_enabled:
        return AlertAction.DISABLED

    if state.tripped_at is not None:
        return AlertAction.SUPPRESSED

    if _incident_count_in_window(session, rep_id, now, burst_window) > burst_threshold:
        state.tripped_at = now
        subject, body = _compose_limit_email(burst_threshold)
        if not _send_rep_email(session, rep_id, subject, body, gmail_service=gmail_service):
            return AlertAction.NOT_CONNECTED
        state.last_email_sent_at = now
        return AlertAction.SENT_LIMIT_NOTICE

    subject, body = _compose_incident_email(source_id, row_ref, reasons)
    if not _send_rep_email(session, rep_id, subject, body, gmail_service=gmail_service):
        return AlertAction.NOT_CONNECTED
    state.last_email_sent_at = now
    return AlertAction.SENT_INCIDENT


@dataclass
class AlertSettingsView:
    enabled: bool
    last_incident_at: datetime | None
    last_email_sent_at: datetime | None


def get_settings_view(session: Session, rep_id: uuid.UUID, since: datetime | None) -> AlertSettingsView:
    """Read model for the Settings panel. `since` is the current
    login's start time (auth.get_login_time_for_signed_token) — a
    display-only filter, not a mutation: an event that happened before
    this login shows as "none since you logged in" here, but the real
    RepInjectionAlertState row backing the rate limiter is untouched,
    so logging out and back in can't be used to dodge the suppression
    cooldown early.
    """
    rep = session.get(Rep, rep_id)
    state = session.get(RepInjectionAlertState, rep_id)

    def _since_login(value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if since is not None and value < since:
            return None
        return value

    if state is None:
        return AlertSettingsView(
            enabled=rep.injection_alerts_enabled if rep else True,
            last_incident_at=None,
            last_email_sent_at=None,
        )
    return AlertSettingsView(
        enabled=rep.injection_alerts_enabled if rep else True,
        last_incident_at=_since_login(state.last_incident_at),
        last_email_sent_at=_since_login(state.last_email_sent_at),
    )


def set_enabled(session: Session, rep_id: uuid.UUID, enabled: bool) -> None:
    rep = session.get(Rep, rep_id)
    if rep is not None:
        rep.injection_alerts_enabled = enabled
