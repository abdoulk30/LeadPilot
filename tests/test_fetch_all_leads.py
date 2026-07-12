"""Real tests against real local Postgres. The connector is faked
(tests/fakes.py) since fetch_all_leads's own logic — dedup, the
run-lock — is what's under test here, not GoogleSheetsConnector's
correctness (that has its own real-API tests). test_live_fetch_all_leads_against_a_real_connected_rep
below is the one test that hits the real thing, auto-skipped until a
rep has a real completed OAuth connection — same pattern as
test_google_sheets_connector_live.py.
"""

import uuid
from datetime import timedelta

import pytest
from sqlalchemy import select

from leadpilot import auth, locks
from leadpilot.connectors.base import LeadRecord
from leadpilot.connectors.google_sheets import GoogleSheetsConnector
from leadpilot.models.dedup import LeadSourceRow
from leadpilot.models.leads import Lead
from leadpilot.models.rep_google_credential import RepGoogleCredential
from leadpilot.tools import fetch_all_leads
from leadpilot.tools.registry import load_all_tools

from fakes import FakeLeadSourceConnector


def _make_rep(session) -> uuid.UUID:
    rep = auth.create_rep(session, email=f"{uuid.uuid4()}-fetch-all-leads-test@example.com", password="testpassword123")
    session.flush()
    return rep.rep_id


def _record(source_id, row_ref, name, phone=None, email=None, company=None, status=None):
    return LeadRecord(
        source_id=source_id, row_ref=row_ref, name=name, phone=phone, email=email, company=company,
        status=status, raw={"Name": name, "Phone": phone or "", "Email": email or "", "Status": status or ""},
    )


def test_registers_as_a_tool():
    tools = load_all_tools()
    assert "fetch_all_leads" in tools
    assert tools["fetch_all_leads"].handler is fetch_all_leads.run


def test_creates_new_leads_from_fresh_rows(db_session):
    rep_id = _make_rep(db_session)
    connector = FakeLeadSourceConnector({
        "sheet_a": [
            _record("sheet_a", "2", "John Doe", phone="555-1111", status="New"),
            _record("sheet_a", "3", "Jane Smith", phone="555-2222", status="New"),
        ]
    })
    results = fetch_all_leads.run(db_session, rep_id, connector=connector)
    assert len(results) == 2
    names = {r["name"] for r in results}
    assert names == {"John Doe", "Jane Smith"}

    leads = db_session.execute(select(Lead)).scalars().all()
    assert len(leads) == 2


def test_dedups_by_phone_across_two_sources(db_session):
    """Eval Case 2: the same lead on two separate intake sheets must
    consolidate into a single canonical record.
    """
    rep_id = _make_rep(db_session)
    connector = FakeLeadSourceConnector({
        "sheet_a": [_record("sheet_a", "2", "Jane Smith", phone="555-2222", status="New")],
        "sheet_b": [_record("sheet_b", "5", "Jane Smith", phone="555-2222", status="Interested")],
    })
    results = fetch_all_leads.run(db_session, rep_id, connector=connector)
    assert len(results) == 2  # two rows returned...
    assert len({r["lead_id"] for r in results}) == 1  # ...but one canonical lead

    leads = db_session.execute(select(Lead)).scalars().all()
    assert len(leads) == 1


def test_dedups_by_email_when_phone_is_absent(db_session):
    rep_id = _make_rep(db_session)
    connector = FakeLeadSourceConnector({
        "sheet_a": [_record("sheet_a", "2", "Aiden B", email="aiden@example.com")],
        "sheet_b": [_record("sheet_b", "9", "Aiden B", email="aiden@example.com")],
    })
    results = fetch_all_leads.run(db_session, rep_id, connector=connector)
    assert len({r["lead_id"] for r in results}) == 1


def test_different_phone_and_email_are_not_merged(db_session):
    rep_id = _make_rep(db_session)
    connector = FakeLeadSourceConnector({
        "sheet_a": [_record("sheet_a", "2", "John Doe", phone="555-1111", email="john@example.com")],
        "sheet_b": [_record("sheet_b", "3", "Someone Else", phone="555-9999", email="else@example.com")],
    })
    results = fetch_all_leads.run(db_session, rep_id, connector=connector)
    assert len({r["lead_id"] for r in results}) == 2


def test_rerun_is_idempotent_not_duplicating_leads(db_session):
    rep_id = _make_rep(db_session)
    connector = FakeLeadSourceConnector({
        "sheet_a": [_record("sheet_a", "2", "John Doe", phone="555-1111", status="New")],
    })
    fetch_all_leads.run(db_session, rep_id, connector=connector)
    fetch_all_leads.run(db_session, rep_id, connector=connector)

    leads = db_session.execute(select(Lead)).scalars().all()
    assert len(leads) == 1
    rows = db_session.execute(select(LeadSourceRow)).scalars().all()
    assert len(rows) == 1


def test_rerun_updates_existing_row_when_source_data_changed(db_session):
    rep_id = _make_rep(db_session)
    connector = FakeLeadSourceConnector({
        "sheet_a": [_record("sheet_a", "2", "John Doe", phone="555-1111", status="New")],
    })
    fetch_all_leads.run(db_session, rep_id, connector=connector)

    # Same row, status changed — simulates the sheet being edited between runs.
    connector._rows_by_source["sheet_a"][0].status = "Contacted"
    connector._rows_by_source["sheet_a"][0].raw["Status"] = "Contacted"
    results = fetch_all_leads.run(db_session, rep_id, connector=connector)

    assert results[0]["status"] == "Contacted"
    rows = db_session.execute(select(LeadSourceRow)).scalars().all()
    assert len(rows) == 1  # still one row, updated in place, not a duplicate
    assert rows[0].raw_data["Status"] == "Contacted"


def test_raises_when_a_run_is_already_in_progress(db_session):
    rep_id = _make_rep(db_session)
    assert locks.acquire_run_lock(db_session, rep_id, run_by="someone-else", stale_after=timedelta(hours=2)) is True
    db_session.commit()

    connector = FakeLeadSourceConnector({"sheet_a": [_record("sheet_a", "2", "John Doe", phone="555-1111")]})
    with pytest.raises(fetch_all_leads.RunAlreadyInProgressError):
        fetch_all_leads.run(db_session, rep_id, connector=connector)


def test_lock_is_released_after_a_successful_run(db_session):
    rep_id = _make_rep(db_session)
    connector = FakeLeadSourceConnector({"sheet_a": [_record("sheet_a", "2", "John Doe", phone="555-1111")]})
    fetch_all_leads.run(db_session, rep_id, connector=connector)

    # If the lock weren't released, this second acquire would fail.
    assert locks.acquire_run_lock(db_session, rep_id, run_by="next-run", stale_after=timedelta(hours=2)) is True


class _BrokenConnector(FakeLeadSourceConnector):
    def fetch_rows(self, source_id):
        raise RuntimeError("simulated failure mid-run")


def test_lock_is_released_even_if_the_run_fails(db_session):
    rep_id = _make_rep(db_session)
    broken = _BrokenConnector({"sheet_a": [_record("sheet_a", "2", "John Doe", phone="555-1111")]})

    with pytest.raises(RuntimeError, match="simulated failure"):
        fetch_all_leads.run(db_session, rep_id, connector=broken)

    assert locks.acquire_run_lock(db_session, rep_id, run_by="recovery-run", stale_after=timedelta(hours=2)) is True


def test_empty_sources_returns_empty_list_not_an_error(db_session):
    """An unconnected rep (or one with no granted files) — list_sources()
    returning [] is a valid state, not an error condition.
    """
    rep_id = _make_rep(db_session)
    connector = FakeLeadSourceConnector({})
    assert fetch_all_leads.run(db_session, rep_id, connector=connector) == []


def test_live_fetch_all_leads_against_a_real_connected_rep(db_session):
    row = db_session.execute(
        select(RepGoogleCredential).where(RepGoogleCredential.revoked_at.is_(None))
    ).scalars().first()
    if row is None or not row.granted_file_ids:
        pytest.skip(
            "No rep has a real, active Google connection with at least one granted "
            "file yet — complete GET /auth/google/connect through a real browser first."
        )
    real_connector = GoogleSheetsConnector(db_session, row.rep_id)
    results = fetch_all_leads.run(db_session, row.rep_id, connector=real_connector)
    assert len(results) > 0
    for result in results:
        assert result["source_id"] in row.granted_file_ids
