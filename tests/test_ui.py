"""Step 3 workspace tests — real HTTP through TestClient, real local
Postgres, fake external clients injected by monkeypatching ui.py's
factory functions (the same DI pattern the tools' own tests use).

Follows test_app.py's committed-data pattern: the app serves requests
from its own sessions (app.get_db), so fixtures commit for real and
clean up after themselves.
"""

import json
import uuid

import pytest
from fastapi.testclient import TestClient

from leadpilot import auth, ui
from leadpilot.app import app
from leadpilot.config import settings
from leadpilot.connectors.base import FieldDiff, LeadRecord
from leadpilot.db import SessionLocal
from leadpilot.models.contact_history import ContactHistory, Outcome, Stage, Tool
from leadpilot.models.dedup import LeadSourceRow
from leadpilot.models.leads import Lead
from leadpilot.models.rep import Rep, RepSession
from leadpilot.tools import (
    dispatch_slack_handoff,
    initiate_lead_call,
    send_lead_email,
    send_lead_text,
    update_lead_sheet,
)

from fakes import FakeLeadSourceConnector


class FakeTwilioClient:
    def __init__(self, status="queued"):
        self.calls = []
        self._status = status
        self.messages = self

    def create(self, *, body, from_, to):
        from types import SimpleNamespace

        self.calls.append({"body": body, "from_": from_, "to": to})
        return SimpleNamespace(sid=f"SM{len(self.calls):032x}", status=self._status)


class FakeSlackClient:
    def __init__(self, ok=True):
        self.calls = []
        self._ok = ok

    def chat_postMessage(self, *, channel, text):
        self.calls.append({"channel": channel, "text": text})
        return {"ok": self._ok, "ts": f"169{len(self.calls)}.0001"}


class Workspace:
    """One committed rep + lead + logged-in client, torn down after."""

    def __init__(self):
        self.session = SessionLocal()
        self.email = f"{uuid.uuid4()}-ui-test@example.com"
        rep = auth.create_rep(self.session, email=self.email, password="testpassword123", display_name="UI Test Rep")
        self.rep_id = rep.rep_id
        lead = Lead(
            display_name="Queue Lead", primary_phone="+15550009999",
            primary_email="queue-lead@example.com", company="Testco",
        )
        self.session.add(lead)
        self.session.flush()
        self.lead_id = lead.lead_id
        self.session.commit()

        self.client = TestClient(app)
        response = self.client.post(
            "/login/form",
            data={"email": self.email, "password": "testpassword123"},
            follow_redirects=False,
        )
        assert response.status_code == 303

    def teardown(self):
        s = SessionLocal()
        lead_ids = [
            row.lead_id
            for row in s.query(LeadSourceRow).filter(LeadSourceRow.source_id.like("ui-test-%")).all()
        ]
        s.query(ContactHistory).filter(
            (ContactHistory.lead_id == self.lead_id) | (ContactHistory.lead_id.in_(lead_ids or [uuid.uuid4()]))
        ).delete(synchronize_session=False)
        s.query(LeadSourceRow).filter_by(lead_id=self.lead_id).delete()
        s.query(Lead).filter_by(lead_id=self.lead_id).delete()
        s.query(RepSession).filter_by(rep_id=self.rep_id).delete()
        s.query(Rep).filter_by(rep_id=self.rep_id).delete()
        s.commit()
        s.close()
        self.session.close()


@pytest.fixture()
def ws():
    workspace = Workspace()
    yield workspace
    workspace.teardown()


def _event(event_id) -> ContactHistory:
    s = SessionLocal()
    try:
        return s.get(ContactHistory, uuid.UUID(str(event_id)))
    finally:
        s.close()


# ---- Auth gating -------------------------------------------------------


def test_workspace_redirects_anonymous_to_login():
    client = TestClient(app)
    response = client.get("/", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_htmx_partial_gets_hx_redirect_when_anonymous():
    client = TestClient(app)
    response = client.get("/ui/queue", headers={"HX-Request": "true"})
    assert response.status_code == 401
    assert response.headers["HX-Redirect"] == "/login"
    assert "Queue Lead" not in response.text  # no data on the reject path


def test_login_form_rejects_bad_credentials():
    client = TestClient(app)
    response = client.post("/login/form", data={"email": "nobody@example.com", "password": "wrong"})
    assert response.status_code == 401
    assert "Invalid email or password" in response.text


def test_login_form_then_workspace_renders(ws):
    response = ws.client.get("/")
    assert response.status_code == 200
    assert "LeadPilot" in response.text
    assert "Prioritized queue" not in response.text  # queue loads via htmx, not inline


# ---- Queue -------------------------------------------------------------


def test_queue_lists_lead_with_rank_pill(ws):
    response = ws.client.get("/ui/queue")
    assert response.status_code == 200
    assert "Queue Lead" in response.text
    assert "R2" in response.text  # no contact yet → Rank 2


def test_urgent_handoff_sorts_lead_to_top_and_badges_it(ws, monkeypatch):
    monkeypatch.setattr(settings, "slack_handoff_channel_ids", "C1,C2,C3")
    s = SessionLocal()
    dispatch_slack_handoff.dispatch_slack_handoff(
        s, lead_id=ws.lead_id, message_type="urgent_callback_request", message="Call them back now."
    )
    s.commit()
    s.close()

    response = ws.client.get("/ui/queue")
    assert "urgent" in response.text


# ---- Approve: text (fake Twilio) ----------------------------------------


def _stage_text(ws, message="Hi there — quick follow-up."):
    s = SessionLocal()
    staged = send_lead_text.send_lead_text(s, lead_id=ws.lead_id, message=message)
    s.commit()
    s.close()
    return staged["event_id"]


def test_approve_text_sends_via_fake_twilio_and_confirms(ws, monkeypatch):
    fake = FakeTwilioClient()
    monkeypatch.setattr(ui, "twilio_client_factory", lambda: fake)
    event_id = _stage_text(ws)

    response = ws.client.post(f"/ui/actions/{event_id}/approve")
    assert response.status_code == 200
    assert "Sent ✓" in response.text
    assert len(fake.calls) == 1
    assert fake.calls[0]["to"] == "+15550009999"

    row = _event(event_id)
    assert row.stage == Stage.EXECUTED
    assert row.outcome == Outcome.DELIVERED
    assert row.rep_id == ws.rep_id  # approval attributed to the clicking rep


def test_second_approve_is_a_noop_single_use(ws, monkeypatch):
    fake = FakeTwilioClient()
    monkeypatch.setattr(ui, "twilio_client_factory", lambda: fake)
    event_id = _stage_text(ws)

    ws.client.post(f"/ui/actions/{event_id}/approve")
    response = ws.client.post(f"/ui/actions/{event_id}/approve")
    assert "already handled" in response.text
    assert len(fake.calls) == 1  # the real point: no double-send


def test_reject_then_approve_never_sends(ws, monkeypatch):
    fake = FakeTwilioClient()
    monkeypatch.setattr(ui, "twilio_client_factory", lambda: fake)
    event_id = _stage_text(ws)

    response = ws.client.post(f"/ui/actions/{event_id}/reject")
    assert "Rejected" in response.text
    ws.client.post(f"/ui/actions/{event_id}/approve")
    assert fake.calls == []
    assert _event(event_id).stage == Stage.REJECTED


# ---- Edit before approval ------------------------------------------------


def test_edit_pending_text_draft_updates_content(ws):
    event_id = _stage_text(ws, message="Original wording")
    response = ws.client.post(f"/ui/actions/{event_id}/edit", data={"body": "Reworded by the rep"})
    assert response.status_code == 200
    assert "Reworded by the rep" in response.text
    assert _event(event_id).content_ref == "Reworded by the rep"


def test_edit_after_execution_does_not_apply(ws, monkeypatch):
    monkeypatch.setattr(ui, "twilio_client_factory", lambda: FakeTwilioClient())
    event_id = _stage_text(ws, message="Sent wording")
    ws.client.post(f"/ui/actions/{event_id}/approve")

    response = ws.client.post(f"/ui/actions/{event_id}/edit", data={"body": "Too late"})
    assert "already handled" in response.text
    assert _event(event_id).content_ref == "Sent wording"


def test_edit_email_updates_subject_and_body_keeps_recipient(ws):
    s = SessionLocal()
    staged = send_lead_email.send_lead_email(
        s, lead_id=ws.lead_id, subject="Old subject", body="Old body"
    )
    s.commit()
    s.close()

    ws.client.post(
        f"/ui/actions/{staged['event_id']}/edit",
        data={"subject": "New subject", "body": "New body"},
    )
    content = json.loads(_event(staged["event_id"]).content_ref)
    assert content == {"subject": "New subject", "body": "New body", "to": "queue-lead@example.com"}


# ---- Approve: call → clipboard + outcome (§6f) ----------------------------


def _stage_call(ws):
    s = SessionLocal()
    staged = initiate_lead_call.initiate_lead_call(s, lead_id=ws.lead_id)
    s.commit()
    s.close()
    return staged["event_id"]


def test_approve_call_copies_number_and_offers_outcome_row(ws):
    event_id = _stage_call(ws)
    response = ws.client.post(f"/ui/actions/{event_id}/approve")
    assert 'data-clipboard-copy="+15550009999"' in response.text
    assert "Nothing dials automatically" not in response.text  # pre-approval note gone
    for label in ("Answered", "No answer", "Voicemail", "Didn&#39;t call"):
        assert label in response.text

    row = _event(event_id)
    assert row.stage == Stage.EXECUTED
    assert row.outcome == Outcome.PENDING  # the log_call_outcome contract


def test_log_outcome_from_card_closes_the_pending_call(ws):
    event_id = _stage_call(ws)
    ws.client.post(f"/ui/actions/{event_id}/approve")

    response = ws.client.post(f"/ui/calls/{event_id}/outcome", data={"outcome": "no_answer"})
    assert "Outcome logged" in response.text
    assert _event(event_id).outcome == Outcome.NO_ANSWER


def test_unlogged_call_shows_in_queue_strip_and_logs_from_it(ws):
    event_id = _stage_call(ws)
    ws.client.post(f"/ui/actions/{event_id}/approve")

    queue = ws.client.get("/ui/queue")
    assert "Unlogged calls (1)" in queue.text
    assert "Rank 3 follow-ups pause" in queue.text

    response = ws.client.post(
        f"/ui/calls/{event_id}/outcome",
        data={"outcome": "didnt_call"},
        headers={"HX-Target": "queue-pane"},
    )
    assert "Unlogged calls" not in response.text  # strip gone once logged
    assert _event(event_id).outcome == Outcome.DIDNT_CALL


# ---- Approve: Slack handoff ------------------------------------------------


def test_approve_urgent_handoff_posts_to_all_channels_same_gate(ws, monkeypatch):
    monkeypatch.setattr(settings, "slack_handoff_channel_ids", "C1,C2,C3")
    fake = FakeSlackClient()
    monkeypatch.setattr(ui, "slack_client_factory", lambda: fake)

    s = SessionLocal()
    staged = dispatch_slack_handoff.dispatch_slack_handoff(
        s, lead_id=ws.lead_id, message_type="urgent_callback_request", message="Urgent callback please."
    )
    s.commit()
    s.close()

    response = ws.client.post(f"/ui/actions/{staged['event_id']}/approve")
    assert "Posted ✓" in response.text
    assert "3 of 3" in response.text
    assert [c["channel"] for c in fake.calls] == ["C1", "C2", "C3"]


# ---- Sheet edits: stage, approve, stale-write recovery (§6e) ---------------


def _sheet_fixture(ws, current="New"):
    source_id = f"ui-test-{uuid.uuid4()}"
    connector = FakeLeadSourceConnector({
        source_id: [LeadRecord(
            source_id=source_id, row_ref="2", name="Queue Lead", phone="+15550009999",
            email="queue-lead@example.com", company="Testco", status=current,
        )]
    })
    s = SessionLocal()
    s.add(LeadSourceRow(source_id=source_id, row_ref="2", lead_id=ws.lead_id,
                        raw_data={"Status": current}))
    s.commit()
    s.close()
    return source_id, connector


def test_stage_edit_then_approve_writes_through_fake_connector(ws, monkeypatch):
    source_id, connector = _sheet_fixture(ws)
    monkeypatch.setattr(ui, "sheets_connector_factory", lambda db, rep_id: connector)

    response = ws.client.post(
        f"/ui/leads/{ws.lead_id}/stage-edit",
        data={"source_row": f"{source_id}|2", "field": "status", "value": "Contacted"},
    )
    assert response.status_code == 200
    assert "Contacted" in response.text  # diff card visible

    s = SessionLocal()
    event = (
        s.query(ContactHistory)
        .filter_by(lead_id=ws.lead_id, tool=Tool.UPDATE_LEAD_SHEET)
        .one()
    )
    s.close()

    approve = ws.client.post(f"/ui/actions/{event.event_id}/approve")
    assert "Written to sheet ✓" in approve.text
    assert connector._writes == [(source_id, "2", "status", "Contacted")]


def test_stale_write_shows_conflict_panel_and_restages_fresh_diff(ws, monkeypatch):
    source_id, connector = _sheet_fixture(ws, current="New")
    monkeypatch.setattr(ui, "sheets_connector_factory", lambda db, rep_id: connector)

    ws.client.post(
        f"/ui/leads/{ws.lead_id}/stage-edit",
        data={"source_row": f"{source_id}|2", "field": "status", "value": "Contacted"},
    )
    s = SessionLocal()
    event = s.query(ContactHistory).filter_by(lead_id=ws.lead_id, tool=Tool.UPDATE_LEAD_SHEET).one()
    event_id = event.event_id
    s.close()

    # Someone edits the sheet out from under the approval.
    connector._rows_by_source[source_id][0].status = "Escalated"

    response = ws.client.post(f"/ui/actions/{event_id}/approve")
    assert "nothing was overwritten" in response.text.lower()
    assert "Escalated" in response.text          # what the sheet now says
    assert "Contacted" in response.text          # what you approved
    assert "Apply my edit anyway" in response.text
    assert connector._writes == []               # blocked, not silently applied
    assert _event(event_id).stage == Stage.EXECUTED  # approval consumed — never replayable

    # "Apply my edit anyway" → a FRESH staged diff against the new baseline.
    restage = ws.client.post(
        f"/ui/leads/{ws.lead_id}/stage-edit",
        data={"source_row": f"{source_id}|2", "field": "status", "value": "Contacted"},
    )
    assert restage.status_code == 200
    s = SessionLocal()
    fresh = (
        s.query(ContactHistory)
        .filter_by(lead_id=ws.lead_id, tool=Tool.UPDATE_LEAD_SHEET, stage=Stage.AWAITING_REP_APPROVAL)
        .one()
    )
    fresh_info = json.loads(fresh.content_ref)
    s.close()
    assert fresh_info["current"] == "Escalated"  # new baseline, not the stale one


# ---- Lead center + rail -----------------------------------------------------


def test_lead_center_shows_pending_cards_and_rail_timeline(ws):
    _stage_text(ws, message="Draft for the center pane")
    response = ws.client.get(f"/ui/leads/{ws.lead_id}")
    assert "Queue Lead" in response.text
    assert "Draft for the center pane" in response.text
    assert "Approve &amp; send text" in response.text
    assert "Contact history" in response.text
    assert "Why this rank" in response.text


def test_pending_call_outcome_reads_as_paused_in_timeline(ws):
    event_id = _stage_call(ws)
    ws.client.post(f"/ui/actions/{event_id}/approve")
    response = ws.client.get(f"/ui/leads/{ws.lead_id}")
    assert "outcome unknown — follow-up logic paused" in response.text


# ---- Search (§6h) ------------------------------------------------------------


class FakeGmailService:
    """Just enough of the Gmail resource chain for search_communications."""

    def __init__(self, messages):
        self._messages = messages

    def users(self):
        return self

    def messages(self):
        return self

    def list(self, *, userId, q):
        return self._resp({"messages": [{"id": m["id"]} for m in self._messages]})

    def get(self, *, userId, id, format, metadataHeaders):
        m = next(x for x in self._messages if x["id"] == id)
        return self._resp({
            "id": id,
            "snippet": m.get("snippet", ""),
            "payload": {
                "headers": [{"name": k, "value": v} for k, v in m.get("headers", {}).items()],
                "parts": m.get("parts", []),
            },
        })

    @staticmethod
    def _resp(data):
        class R:
            def execute(self_inner):
                return data
        return R()


def test_search_by_name_shows_identity_and_email_only_notice(ws, monkeypatch):
    fake_gmail = FakeGmailService([
        {"id": "m1", "snippet": "about your bank statements",
         "headers": {"From": "queue-lead@example.com", "To": "me@rep.com",
                     "Subject": "Bank statements", "Date": "Mon, 13 Jul 2026"}},
    ])
    monkeypatch.setattr(ui, "gmail_service_factory", lambda: fake_gmail)

    response = ws.client.get("/ui/search/results", params={"q": "Queue Lead"})
    assert response.status_code == 200
    assert "Queue Lead" in response.text                      # identity header
    assert "texts can't be searched by name" in response.text  # amber truth notice
    assert "Bank statements" in response.text


def test_search_when_not_connected_shows_connect_prompt(ws):
    # No factory override, no stored Google credential → the tool's own
    # RepNotConnectedError surfaces as guidance, not a stack trace.
    response = ws.client.get("/ui/search/results", params={"q": "queue-lead@example.com"})
    assert response.status_code == 200
    assert "Connect your Google account" in response.text


# ---- Reset all lead data (2026-07-15) ----------------------------------


def test_reset_requires_typed_confirmation(ws):
    response = ws.client.post("/ui/reset-data", data={"confirm": "yes"})
    assert "nothing was deleted" in response.text
    s = SessionLocal()
    assert s.get(Lead, ws.lead_id) is not None
    s.close()


def test_reset_wipes_leads_but_keeps_rep(ws):
    _stage_text(ws, message="Doomed draft")
    response = ws.client.post("/ui/reset-data", data={"confirm": "RESET"})
    assert response.status_code == 200
    assert "Wiped:" in response.text
    assert response.headers.get("HX-Trigger") == "leads-changed"

    s = SessionLocal()
    assert s.get(Lead, ws.lead_id) is None
    assert s.query(ContactHistory).count() == 0
    assert s.get(Rep, ws.rep_id) is not None  # login survives
    s.close()


def test_reset_requires_auth():
    client = TestClient(app)
    response = client.post("/ui/reset-data", data={"confirm": "RESET"}, headers={"HX-Request": "true"})
    assert response.status_code == 401
