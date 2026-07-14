"""Real tests against real local Postgres. run() (staging) uses the same
fake-connector pattern as fetch_all_leads/fetch_ad_hoc_sheet. execute()'s
concurrency test uses real separate SessionLocal() connections (same
pattern as test_gate.py's test_try_execute_is_single_use_under_concurrency)
since the whole point is proving Postgres row-locking, not app logic.
test_live_execute_against_a_real_connected_rep is the one test that
performs a real Sheets write, auto-skipped until a rep has a real
completed OAuth connection with at least one granted file.
"""

import threading
import uuid

from leadpilot import auth, gate
from leadpilot.connectors.base import LeadRecord, StaleWriteError
from leadpilot.connectors.google_sheets import GoogleSheetsConnector
from leadpilot.db import SessionLocal
from leadpilot.models.contact_history import Channel, ContactHistory, Stage, Tool
from leadpilot.models.leads import Lead
from leadpilot.models.rep import Rep
from leadpilot.models.rep_google_credential import RepGoogleCredential
from leadpilot.tools import update_lead_sheet
from leadpilot.tools.registry import load_all_tools

from fakes import FakeLeadSourceConnector

from sqlalchemy import select

import pytest


def _make_rep(session, email: str = "update-lead-sheet-test@example.com") -> uuid.UUID:
    rep = auth.create_rep(session, email=f"{uuid.uuid4()}-{email}", password="testpassword123")
    session.flush()
    return rep.rep_id


def _make_lead(session, **kwargs) -> uuid.UUID:
    lead = Lead(display_name="Test Lead", **kwargs)
    session.add(lead)
    session.flush()
    return lead.lead_id


def _record(source_id, row_ref, name, phone=None, email=None, company=None, status=None):
    return LeadRecord(
        source_id=source_id, row_ref=row_ref, name=name, phone=phone, email=email, company=company,
        status=status, raw={"Name": name, "Phone": phone or "", "Email": email or "", "Status": status or ""},
    )


def test_registers_as_a_tool():
    tools = load_all_tools()
    assert "update_lead_sheet" in tools
    assert tools["update_lead_sheet"].handler is update_lead_sheet.run


def test_run_stages_a_diff_without_writing(db_session):
    rep_id = _make_rep(db_session)
    lead_id = _make_lead(db_session)
    connector = FakeLeadSourceConnector({
        "sheet_1": [_record("sheet_1", "2", "John Doe", status="New")],
    })

    result = update_lead_sheet.run(
        db_session, rep_id, lead_id, "sheet_1", "2", "status", "Contacted", connector=connector
    )

    assert result["status"] == "awaiting_rep_approval"
    assert result["current"] == "New"
    assert result["proposed"] == "Contacted"
    assert connector._writes == []  # nothing actually written yet

    event = db_session.get(ContactHistory, uuid.UUID(result["event_id"]))
    assert event.stage == Stage.AWAITING_REP_APPROVAL
    assert event.tool == Tool.UPDATE_LEAD_SHEET
    assert event.channel == Channel.SHEET_EDIT
    assert event.lead_id == lead_id


def test_execute_does_nothing_without_approval(db_session):
    rep_id = _make_rep(db_session)
    lead_id = _make_lead(db_session)
    connector = FakeLeadSourceConnector({
        "sheet_1": [_record("sheet_1", "2", "John Doe", status="New")],
    })
    staged = update_lead_sheet.run(
        db_session, rep_id, lead_id, "sheet_1", "2", "status", "Contacted", connector=connector
    )

    result = update_lead_sheet.execute(db_session, uuid.UUID(staged["event_id"]), connector=connector)

    assert result == {"executed": False}
    assert connector._writes == []


def test_execute_writes_after_approval(db_session):
    rep_id = _make_rep(db_session)
    lead_id = _make_lead(db_session)
    connector = FakeLeadSourceConnector({
        "sheet_1": [_record("sheet_1", "2", "John Doe", status="New")],
    })
    staged = update_lead_sheet.run(
        db_session, rep_id, lead_id, "sheet_1", "2", "status", "Contacted", connector=connector
    )
    event_id = uuid.UUID(staged["event_id"])
    assert gate.approve(db_session, event_id, rep_id=rep_id) is True

    result = update_lead_sheet.execute(db_session, event_id, connector=connector)

    assert result["executed"] is True
    assert result == {
        "executed": True, "source_id": "sheet_1", "row_ref": "2", "field": "status", "value": "Contacted",
    }
    assert connector._writes == [("sheet_1", "2", "status", "Contacted")]

    event = db_session.get(ContactHistory, event_id)
    assert event.stage == Stage.EXECUTED


def test_execute_raises_stale_write_error_if_the_cell_changed_since_staging(db_session):
    """Decision 034: the rep approved a diff built from "New", but the
    cell changed to something else in between (another rep's edit, or
    a direct edit in Google's UI) — execute() must not silently
    overwrite it. Confirms both that update_lead_sheet actually passes
    expected_current through, and that StaleWriteError isn't swallowed
    into the generic WriteExecutionFailedAfterApprovalError.
    """
    rep_id = _make_rep(db_session)
    lead_id = _make_lead(db_session)
    connector = FakeLeadSourceConnector({
        "sheet_1": [_record("sheet_1", "2", "John Doe", status="New")],
    })
    staged = update_lead_sheet.run(
        db_session, rep_id, lead_id, "sheet_1", "2", "status", "Contacted", connector=connector
    )
    event_id = uuid.UUID(staged["event_id"])
    gate.approve(db_session, event_id, rep_id=rep_id)

    # Simulate the cell changing after the rep reviewed the diff but
    # before it executes — e.g. someone edited the sheet directly.
    connector._rows_by_source["sheet_1"][0].status = "Already Contacted By Someone Else"

    with pytest.raises(StaleWriteError):
        update_lead_sheet.execute(db_session, event_id, connector=connector)

    # The gate was still consumed (single-use survives a failed write —
    # same trade-off WriteExecutionFailedAfterApprovalError documents),
    # but the sheet itself was never actually overwritten.
    event = db_session.get(ContactHistory, event_id)
    assert event.stage == Stage.EXECUTED
    assert connector._writes == []
    assert connector._rows_by_source["sheet_1"][0].status == "Already Contacted By Someone Else"


def test_execute_is_single_use_after_approval(db_session):
    rep_id = _make_rep(db_session)
    lead_id = _make_lead(db_session)
    connector = FakeLeadSourceConnector({
        "sheet_1": [_record("sheet_1", "2", "John Doe", status="New")],
    })
    staged = update_lead_sheet.run(
        db_session, rep_id, lead_id, "sheet_1", "2", "status", "Contacted", connector=connector
    )
    event_id = uuid.UUID(staged["event_id"])
    gate.approve(db_session, event_id, rep_id=rep_id)

    first = update_lead_sheet.execute(db_session, event_id, connector=connector)
    second = update_lead_sheet.execute(db_session, event_id, connector=connector)

    assert first["executed"] is True
    assert second == {"executed": False}
    assert len(connector._writes) == 1  # the sheet was only written once


def test_execute_raises_for_a_non_update_lead_sheet_event(db_session):
    """execute() must not blindly trust event_id belongs to it — a
    stray call with another tool's event_id (e.g. initiate_lead_call's)
    should fail loudly, not misinterpret content_ref as JSON.
    """
    lead_id = _make_lead(db_session)
    rep_id = _make_rep(db_session)
    other_event = gate.create_draft(
        db_session, lead_id=lead_id, channel=Channel.CALL, tool=Tool.INITIATE_LEAD_CALL
    )
    gate.approve(db_session, other_event.event_id, rep_id=rep_id)

    with pytest.raises(ValueError, match="not an update_lead_sheet draft"):
        update_lead_sheet.execute(db_session, other_event.event_id)


def test_execute_raises_for_an_unknown_event(db_session):
    with pytest.raises(ValueError, match="No such contact_history event"):
        update_lead_sheet.execute(db_session, uuid.uuid4())


def test_execute_is_single_use_under_concurrency():
    """Same rigor as test_gate.py's try_execute concurrency test — 10
    real, separate DB connections all try to execute() the same
    approved event simultaneously, and the underlying fake connector
    must only ever see one real write.
    """
    setup = SessionLocal()
    rep_id = _make_rep(setup)
    lead_id = _make_lead(setup)
    connector = FakeLeadSourceConnector({
        "sheet_1": [_record("sheet_1", "2", "John Doe", status="New")],
    })
    staged = update_lead_sheet.run(
        setup, rep_id, lead_id, "sheet_1", "2", "status", "Contacted", connector=connector
    )
    event_id = uuid.UUID(staged["event_id"])
    gate.approve(setup, event_id, rep_id=rep_id)
    setup.commit()
    setup.close()

    results: list[dict] = []
    results_lock = threading.Lock()

    def attempt():
        session = SessionLocal()
        try:
            result = update_lead_sheet.execute(session, event_id, connector=connector)
            with results_lock:
                results.append(result)
        finally:
            session.close()

    threads = [threading.Thread(target=attempt) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    try:
        executed = [r for r in results if r.get("executed") is True]
        assert len(executed) == 1, f"expected exactly one winner, got {len(executed)}"
        assert len(connector._writes) == 1
    finally:
        cleanup = SessionLocal()
        cleanup.query(ContactHistory).filter_by(event_id=event_id).delete()
        cleanup.query(Lead).filter_by(lead_id=lead_id).delete()
        cleanup.query(Rep).filter_by(rep_id=rep_id).delete()
        cleanup.commit()
        cleanup.close()


def test_live_execute_against_a_real_connected_rep(db_session):
    row = db_session.execute(
        select(RepGoogleCredential).where(RepGoogleCredential.revoked_at.is_(None))
    ).scalars().first()
    if row is None or not row.granted_file_ids:
        pytest.skip(
            "No rep has a real, active Google connection with at least one granted "
            "file yet — complete GET /auth/google/connect through a real browser first."
        )
    real_connector = GoogleSheetsConnector(db_session, row.rep_id)
    source_id = row.granted_file_ids[0]

    rows = real_connector.fetch_rows(source_id)
    assert len(rows) > 0
    target = rows[0]
    lead_id = _make_lead(db_session)

    # Round-trip the status field back to its current value — a real
    # write against the real sheet, but a no-op in content so re-running
    # this test doesn't keep mutating the sheet's actual data.
    staged = update_lead_sheet.run(
        db_session, row.rep_id, lead_id, source_id, target.row_ref, "status",
        target.status or "", connector=real_connector,
    )
    event_id = uuid.UUID(staged["event_id"])
    assert gate.approve(db_session, event_id, rep_id=row.rep_id) is True

    result = update_lead_sheet.execute(db_session, event_id, connector=real_connector)
    assert result["executed"] is True
