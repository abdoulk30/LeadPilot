"""Real tests against the real local Postgres, plus a fake Gmail
service for the send half — same pattern as test_send_lead_email.py's
FakeGmailService. burst_threshold/burst_window/silence_to_resume are
passed as trivial override values (timedelta(seconds=0), etc.) rather
than mocking datetime.now — same convention tests/test_locks.py uses
for cooldown/stale_after.
"""

import uuid
from datetime import timedelta

from sqlalchemy import select

from leadpilot import auth, injection_alerts
from leadpilot.models.injection_alert import InjectionIncident, RepInjectionAlertState
from leadpilot.models.rep import Rep


class FakeGmailService:
    def __init__(self, email_address: str = "rep@gmail.example.com"):
        self.sent: list[dict] = []
        self._email_address = email_address

    def users(self):
        return self

    def messages(self):
        return self

    def getProfile(self, *, userId):
        return self

    def send(self, *, userId, body):
        self.sent.append(body)
        return self

    def execute(self):
        if self.sent:
            return {"id": "msg123"}
        return {"emailAddress": self._email_address}


def _make_rep(session) -> uuid.UUID:
    rep = auth.create_rep(session, email=f"{uuid.uuid4()}-alerts@example.com", password="testpassword123")
    session.flush()
    return rep.rep_id


def _record_incident(session, rep_id, gmail_service=None, **overrides):
    return injection_alerts.record_incident_and_maybe_notify(
        session, rep_id, "sheet_1", "2", {"name": "instruction-override phrasing"},
        gmail_service=gmail_service, **overrides,
    )


def test_incident_is_always_logged_even_when_disabled(db_session):
    rep_id = _make_rep(db_session)
    rep = db_session.get(Rep, rep_id)
    rep.injection_alerts_enabled = False
    db_session.flush()

    action = _record_incident(db_session, rep_id, gmail_service=FakeGmailService())

    assert action == injection_alerts.AlertAction.DISABLED
    incidents = db_session.execute(
        select(InjectionIncident).where(InjectionIncident.rep_id == rep_id)
    ).scalars().all()
    assert len(incidents) == 1
    assert incidents[0].reasons == {"name": "instruction-override phrasing"}


def test_sends_one_email_per_incident_when_enabled(db_session):
    rep_id = _make_rep(db_session)
    fake = FakeGmailService()

    action = _record_incident(db_session, rep_id, gmail_service=fake)

    assert action == injection_alerts.AlertAction.SENT_INCIDENT
    assert len(fake.sent) == 1

    state = db_session.get(RepInjectionAlertState, rep_id)
    assert state.last_incident_at is not None
    assert state.last_email_sent_at is not None
    assert state.tripped_at is None


def test_bundles_multiple_flagged_fields_into_one_incident(db_session):
    """A row with several flagged fields is one incident, not several —
    otherwise a single malicious row could burn through the burst
    budget by itself.
    """
    rep_id = _make_rep(db_session)
    fake = FakeGmailService()

    action = injection_alerts.record_incident_and_maybe_notify(
        db_session, rep_id, "sheet_1", "2",
        {"name": "instruction-override phrasing", "phone": "names an internal tool"},
        gmail_service=fake,
    )

    assert action == injection_alerts.AlertAction.SENT_INCIDENT
    assert len(fake.sent) == 1
    incidents = db_session.execute(
        select(InjectionIncident).where(InjectionIncident.rep_id == rep_id)
    ).scalars().all()
    assert len(incidents) == 1
    assert incidents[0].reasons == {"name": "instruction-override phrasing", "phone": "names an internal tool"}


def test_more_than_threshold_in_window_trips_breaker_and_sends_limit_notice(db_session):
    rep_id = _make_rep(db_session)

    for _ in range(5):
        action = _record_incident(db_session, rep_id, gmail_service=FakeGmailService(), burst_threshold=5)
        assert action == injection_alerts.AlertAction.SENT_INCIDENT

    limit_fake = FakeGmailService()
    action = _record_incident(db_session, rep_id, gmail_service=limit_fake, burst_threshold=5)
    assert action == injection_alerts.AlertAction.SENT_LIMIT_NOTICE
    assert len(limit_fake.sent) == 1

    state = db_session.get(RepInjectionAlertState, rep_id)
    assert state.tripped_at is not None


def test_suppressed_after_breaker_trips_sends_no_further_emails(db_session):
    rep_id = _make_rep(db_session)
    for _ in range(6):
        _record_incident(db_session, rep_id, gmail_service=FakeGmailService(), burst_threshold=5)

    fake = FakeGmailService()
    action = _record_incident(db_session, rep_id, gmail_service=fake, burst_threshold=5)
    assert action == injection_alerts.AlertAction.SUPPRESSED
    assert fake.sent == []


def test_breaker_resumes_after_silence_window(db_session):
    """Once the trailing gap between incidents exceeds silence_to_resume,
    the next incident un-trips the breaker and resumes normal per-
    incident emails — checked lazily on arrival, not via a timer.

    burst_threshold=0 trips the breaker on a single incident (isolating
    "does resume work" from "does the burst count itself decay"), and
    the resuming call's burst_window=timedelta(seconds=0) excludes that
    earlier incident from the recount entirely, so this test only
    exercises the silence-based untrip, not window decay.
    """
    rep_id = _make_rep(db_session)
    action = _record_incident(db_session, rep_id, gmail_service=FakeGmailService(), burst_threshold=0)
    assert action == injection_alerts.AlertAction.SENT_LIMIT_NOTICE

    state = db_session.get(RepInjectionAlertState, rep_id)
    assert state.tripped_at is not None

    fake = FakeGmailService()
    action = _record_incident(
        db_session, rep_id, gmail_service=fake, burst_threshold=5,
        burst_window=timedelta(seconds=0), silence_to_resume=timedelta(seconds=0),
    )
    assert action == injection_alerts.AlertAction.SENT_INCIDENT
    assert len(fake.sent) == 1

    db_session.refresh(state)
    assert state.tripped_at is None


def test_not_connected_returns_gracefully_without_raising(db_session):
    rep_id = _make_rep(db_session)
    action = _record_incident(db_session, rep_id, gmail_service=None)
    assert action == injection_alerts.AlertAction.NOT_CONNECTED
    incidents = db_session.execute(
        select(InjectionIncident).where(InjectionIncident.rep_id == rep_id)
    ).scalars().all()
    assert len(incidents) == 1


def test_settings_view_display_resets_since_login_without_touching_real_state(db_session):
    from datetime import datetime, timezone

    rep_id = _make_rep(db_session)
    _record_incident(db_session, rep_id, gmail_service=FakeGmailService())

    real_state = db_session.get(RepInjectionAlertState, rep_id)
    assert real_state.last_incident_at is not None

    future_login = datetime.now(timezone.utc) + timedelta(seconds=1)
    view = injection_alerts.get_settings_view(db_session, rep_id, since=future_login)
    assert view.last_incident_at is None
    assert view.last_email_sent_at is None

    # The real underlying state row is untouched by the display filter.
    db_session.refresh(real_state)
    assert real_state.last_incident_at is not None

    view_no_filter = injection_alerts.get_settings_view(db_session, rep_id, since=None)
    assert view_no_filter.last_incident_at is not None


def test_set_enabled_toggles_the_rep_setting(db_session):
    from leadpilot.models.rep import Rep

    rep_id = _make_rep(db_session)
    injection_alerts.set_enabled(db_session, rep_id, False)
    assert db_session.get(Rep, rep_id).injection_alerts_enabled is False
    injection_alerts.set_enabled(db_session, rep_id, True)
    assert db_session.get(Rep, rep_id).injection_alerts_enabled is True
