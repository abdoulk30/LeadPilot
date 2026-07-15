"""Step 4 agent-loop tests — real Postgres, real Step 2 tools, scripted
fake model client (per testing/ci-strategy.md: never a real Anthropic
call in CI; the live path is scripts/run_evals.py).

The fake client replays a scripted sequence of responses, which lets
these tests assert the loop's guard behavior exactly: rep_id override,
LeadActionLock double-draft rejection, error tool_results, structural
absence of execute paths, and the runner's lock/report bookkeeping.
"""

import json
import uuid
from types import SimpleNamespace

import pytest

from leadpilot import agent_loop, agent_run, auth, locks
from leadpilot.agent_loop import AgentRunError, run_agent_for_rep
from leadpilot.db import SessionLocal
from leadpilot.models.agent_run_report import AgentRunReport
from leadpilot.models.contact_history import ContactHistory, Stage, Tool
from leadpilot.models.dedup import LeadSourceRow
from leadpilot.models.leads import Lead
from leadpilot.models.rep import Rep, RepSession
from leadpilot.models.run_lock import AgentRunLock, LeadActionLock
from leadpilot.config import settings

from fakes import FakeLeadSourceConnector
from leadpilot.connectors.base import LeadRecord


# ---- Fake Anthropic client ------------------------------------------------


def _block(**kw):
    return SimpleNamespace(**kw)


def text_response(text, stop_reason="end_turn"):
    return SimpleNamespace(
        stop_reason=stop_reason,
        content=[_block(type="text", text=text)],
        usage=SimpleNamespace(input_tokens=100, output_tokens=50),
    )


def tool_response(*calls):
    return SimpleNamespace(
        stop_reason="tool_use",
        content=[
            _block(type="tool_use", id=f"toolu_{i}", name=name, input=inp)
            for i, (name, inp) in enumerate(calls)
        ],
        usage=SimpleNamespace(input_tokens=100, output_tokens=50),
    )


REPORT_JSON = json.dumps({"prioritized_queue": [], "pending_backoffice_handoffs": []})


class FakeAnthropicClient:
    """Replays scripted responses and records every request payload."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.requests = []
        self.messages = self

    def create(self, **kwargs):
        self.requests.append(kwargs)
        if not self._responses:
            raise AssertionError("Fake client ran out of scripted responses")
        return self._responses.pop(0)


# ---- Fixtures ---------------------------------------------------------------


@pytest.fixture()
def rep(db_session):
    return auth.create_rep(
        db_session, email=f"{uuid.uuid4()}-agent-test@example.com", password="testpassword123"
    )


@pytest.fixture()
def lead(db_session):
    row = Lead(
        display_name="Agent Test Lead", primary_phone="+15550007777",
        primary_email="agent-lead@example.com", company="Loopco",
    )
    db_session.add(row)
    db_session.flush()
    return row


# ---- Tool surface -----------------------------------------------------------


def test_batch_tool_list_is_exactly_steps_1_to_6():
    names = [t["name"] for t in agent_loop.build_api_tools()]
    assert names == list(agent_loop.BATCH_TOOL_NAMES)
    # The rep-session tools must NOT be offered to the unattended run —
    # log_call_outcome especially (gate-free by design, rep-only).
    for forbidden in ("log_call_outcome", "update_lead_sheet", "search_communications", "fetch_ad_hoc_sheet"):
        assert forbidden not in names


def test_unknown_tool_is_rejected(db_session, rep):
    with pytest.raises(ValueError, match="not available"):
        agent_loop.dispatch_tool_call(db_session, rep.rep_id, "log_call_outcome", {})


# ---- DATA ACCESS GUARD ------------------------------------------------------


def test_fetch_all_leads_ignores_model_supplied_rep_id(db_session, rep, monkeypatch):
    """The model names ANOTHER rep's UUID; the connector must still be
    built for the run's own rep.
    """
    other_rep_id = uuid.uuid4()
    seen = {}

    def factory(session, rep_id):
        seen["rep_id"] = rep_id
        return FakeLeadSourceConnector({})

    monkeypatch.setattr(agent_loop, "sheets_connector_factory", factory)
    agent_loop.dispatch_tool_call(
        db_session, rep.rep_id, "fetch_all_leads", {"rep_id": str(other_rep_id)}
    )
    assert seen["rep_id"] == rep.rep_id
    assert seen["rep_id"] != other_rep_id


# ---- Duplicate-contact guard (Decision 007) ---------------------------------


def test_second_outreach_draft_for_same_lead_is_blocked(db_session, rep, lead):
    first = agent_loop.dispatch_tool_call(
        db_session, rep.rep_id, "send_lead_text", {"lead_id": str(lead.lead_id), "message": "Hi!"}
    )
    assert json.loads(first)["stage"] == "awaiting_rep_approval"

    with pytest.raises(ValueError, match="already staged or committed within the cooldown"):
        agent_loop.dispatch_tool_call(
            db_session, rep.rep_id, "initiate_lead_call", {"lead_id": str(lead.lead_id)}
        )

    drafts = db_session.query(ContactHistory).filter_by(lead_id=lead.lead_id).count()
    assert drafts == 1  # the block prevented a second draft

    # Cleanup (dispatch commits for real — the rollback fixture can't undo it)
    db_session.query(ContactHistory).filter_by(lead_id=lead.lead_id).delete()
    db_session.query(LeadActionLock).filter_by(lead_id=lead.lead_id).delete()
    db_session.query(Lead).filter_by(lead_id=lead.lead_id).delete()
    db_session.query(Rep).filter_by(rep_id=rep.rep_id).delete()
    db_session.commit()


def test_slack_handoff_is_not_cooldown_gated(db_session, rep, lead, monkeypatch):
    """Back-office handoffs are internal, not lead contact — an urgent
    handoff right after a text draft must not be blocked.
    """
    monkeypatch.setattr(settings, "slack_handoff_channel_ids", "C1,C2,C3")
    agent_loop.dispatch_tool_call(
        db_session, rep.rep_id, "send_lead_text", {"lead_id": str(lead.lead_id), "message": "Hi!"}
    )
    result = agent_loop.dispatch_tool_call(
        db_session, rep.rep_id, "dispatch_slack_handoff",
        {"lead_id": str(lead.lead_id), "message_type": "urgent_callback_request", "message": "Call back"},
    )
    assert json.loads(result)["stage"] == "awaiting_rep_approval"

    db_session.query(ContactHistory).filter_by(lead_id=lead.lead_id).delete()
    db_session.query(LeadActionLock).filter_by(lead_id=lead.lead_id).delete()
    db_session.query(Lead).filter_by(lead_id=lead.lead_id).delete()
    db_session.query(Rep).filter_by(rep_id=rep.rep_id).delete()
    db_session.commit()


# ---- Loop mechanics ----------------------------------------------------------


def _scripted_full_run(rep, lead_source_rows):
    """fetch → history → draft text → final report."""
    lead_id = lead_source_rows
    return [
        tool_response(("fetch_all_leads", {"rep_id": str(rep.rep_id)})),
        tool_response(("get_contact_history", {"lead_id": str(lead_id)})),
        tool_response(("send_lead_text", {"lead_id": str(lead_id), "message": "Quick intro text"})),
        text_response(REPORT_JSON),
    ]


def test_full_scripted_run_stages_real_draft_and_returns_report(db_session, rep, monkeypatch):
    source_id = f"agent-src-{uuid.uuid4()}"
    connector = FakeLeadSourceConnector({
        source_id: [LeadRecord(
            source_id=source_id, row_ref="2", name="Scripted Lead", phone="+15550008888",
            email="scripted@example.com", company="Fakeco", status="New",
        )]
    })
    monkeypatch.setattr(agent_loop, "sheets_connector_factory", lambda s, r: connector)

    # First scripted turn fetches; we need the lead_id it creates for
    # later turns, so run fetch first to learn it, then script fully.
    from leadpilot.tools import fetch_all_leads as fal
    rows = fal.run(db_session, rep.rep_id, connector=connector)
    lead_id = rows[0]["lead_id"]

    fake = FakeAnthropicClient(_scripted_full_run(rep, lead_id))
    result = run_agent_for_rep(db_session, rep.rep_id, anthropic_client=fake)

    assert result.report == {"prioritized_queue": [], "pending_backoffice_handoffs": []}
    assert result.iterations == 4
    assert result.tool_calls == ["fetch_all_leads", "get_contact_history", "send_lead_text"]

    # The draft is REAL — a gate row awaiting approval, never executed.
    event = (
        db_session.query(ContactHistory)
        .filter_by(lead_id=uuid.UUID(lead_id), tool=Tool.SEND_LEAD_TEXT)
        .one()
    )
    assert event.stage == Stage.AWAITING_REP_APPROVAL

    # System prompt went out frozen with a cache breakpoint, tools listed.
    first_request = fake.requests[0]
    assert first_request["system"][0]["text"] == agent_loop.SYSTEM_PROMPT
    assert first_request["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert [t["name"] for t in first_request["tools"]] == list(agent_loop.BATCH_TOOL_NAMES)

    # Cleanup committed rows
    db_session.query(ContactHistory).filter_by(lead_id=uuid.UUID(lead_id)).delete()
    db_session.query(LeadActionLock).filter_by(lead_id=uuid.UUID(lead_id)).delete()
    db_session.query(LeadSourceRow).filter_by(source_id=source_id).delete()
    db_session.query(Lead).filter_by(lead_id=uuid.UUID(lead_id)).delete()
    db_session.query(AgentRunLock).filter_by(rep_id=rep.rep_id).delete()
    db_session.query(Rep).filter_by(rep_id=rep.rep_id).delete()
    db_session.commit()


def test_tool_error_becomes_is_error_result_and_run_continues(db_session, rep, monkeypatch):
    monkeypatch.setattr(agent_loop, "sheets_connector_factory", lambda s, r: FakeLeadSourceConnector({}))
    fake = FakeAnthropicClient([
        tool_response(("get_contact_history", {"lead_id": str(uuid.uuid4())})),  # unknown lead → []
        tool_response(("send_lead_text", {"lead_id": str(uuid.uuid4()), "message": "hi"})),  # no such lead → error
        text_response(REPORT_JSON),
    ])
    result = run_agent_for_rep(db_session, rep.rep_id, anthropic_client=fake)
    assert result.report["prioritized_queue"] == []

    # The failed call produced an is_error tool_result, not a crash.
    error_message = fake.requests[2]["messages"][-1]
    assert error_message["content"][0]["is_error"] is True
    assert "Error:" in error_message["content"][0]["content"]


def test_refusal_raises_agent_run_error(db_session, rep):
    fake = FakeAnthropicClient([text_response("", stop_reason="refusal")])
    with pytest.raises(AgentRunError, match="refusal"):
        run_agent_for_rep(db_session, rep.rep_id, anthropic_client=fake)


def test_non_json_final_output_raises(db_session, rep):
    fake = FakeAnthropicClient([text_response("Here's a summary in prose, no JSON.")])
    with pytest.raises(AgentRunError, match="OUTPUT FORMAT"):
        run_agent_for_rep(db_session, rep.rep_id, anthropic_client=fake)


def test_fenced_json_report_is_tolerated(db_session, rep):
    fake = FakeAnthropicClient([text_response(f"```json\n{REPORT_JSON}\n```")])
    result = run_agent_for_rep(db_session, rep.rep_id, anthropic_client=fake)
    assert result.report["prioritized_queue"] == []


def test_iteration_runaway_is_stopped(db_session, rep, monkeypatch):
    monkeypatch.setattr(agent_loop, "MAX_ITERATIONS", 3)
    fake = FakeAnthropicClient([
        tool_response(("get_contact_history", {"lead_id": str(uuid.uuid4())}))
        for _ in range(4)
    ])
    with pytest.raises(AgentRunError, match="exceeded 3 iterations"):
        run_agent_for_rep(db_session, rep.rep_id, anthropic_client=fake)


# ---- Runner (lock + report bookkeeping) --------------------------------------


def _committed_rep():
    session = SessionLocal()
    rep = auth.create_rep(
        session, email=f"{uuid.uuid4()}-runner-test@example.com", password="testpassword123"
    )
    session.commit()
    rep_id = rep.rep_id
    session.close()
    return rep_id


def _cleanup_rep(rep_id):
    s = SessionLocal()
    s.query(AgentRunReport).filter_by(rep_id=rep_id).delete()
    s.query(AgentRunLock).filter_by(rep_id=rep_id).delete()
    s.query(RepSession).filter_by(rep_id=rep_id).delete()
    s.query(Rep).filter_by(rep_id=rep_id).delete()
    s.commit()
    s.close()


def test_runner_records_success_and_releases_lock():
    rep_id = _committed_rep()
    try:
        session = SessionLocal()
        rep = session.get(Rep, rep_id)
        fake = FakeAnthropicClient([text_response(REPORT_JSON)])
        report = agent_run.run_for_rep(session, rep, anthropic_client=fake)

        assert report.status == "succeeded"
        assert report.report == {"prioritized_queue": [], "pending_backoffice_handoffs": []}
        assert report.iterations == 1
        assert report.finished_at is not None

        lock = session.get(AgentRunLock, rep_id)
        assert lock.locked_at is None  # released
        session.close()
    finally:
        _cleanup_rep(rep_id)


def test_runner_skips_when_lock_already_held():
    rep_id = _committed_rep()
    try:
        holder = SessionLocal()
        assert locks.acquire_run_lock(
            holder, rep_id, run_by="another-run", stale_after=agent_run.RUN_LOCK_STALE_AFTER
        )
        holder.commit()

        session = SessionLocal()
        rep = session.get(Rep, rep_id)
        fake = FakeAnthropicClient([])  # must never be called
        report = agent_run.run_for_rep(session, rep, anthropic_client=fake)
        assert report.status == "skipped_already_running"
        assert fake.requests == []
        session.close()

        locks.release_run_lock(holder, rep_id, run_by="another-run")
        holder.commit()
        holder.close()
    finally:
        _cleanup_rep(rep_id)


def test_runner_records_failure_and_still_releases_lock():
    rep_id = _committed_rep()
    try:
        session = SessionLocal()
        rep = session.get(Rep, rep_id)
        fake = FakeAnthropicClient([text_response("not json at all")])
        report = agent_run.run_for_rep(session, rep, anthropic_client=fake)

        assert report.status == "failed"
        assert "OUTPUT FORMAT" in report.error
        lock = session.get(AgentRunLock, rep_id)
        assert lock.locked_at is None
        session.close()
    finally:
        _cleanup_rep(rep_id)


def test_fetch_all_leads_inside_held_lock_does_not_false_positive(db_session, rep, monkeypatch):
    """The Step 4 reconciliation: runner holds the rep's lock; the
    agent's fetch_all_leads call must not see it as an overlapping run.
    """
    assert locks.acquire_run_lock(db_session, rep.rep_id, run_by="outer", stale_after=agent_run.RUN_LOCK_STALE_AFTER)

    monkeypatch.setattr(agent_loop, "sheets_connector_factory", lambda s, r: FakeLeadSourceConnector({}))
    result = agent_loop.dispatch_tool_call(db_session, rep.rep_id, "fetch_all_leads", {})
    assert json.loads(result) == []
