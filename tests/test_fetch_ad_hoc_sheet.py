"""Real tests against real local Postgres. Same fake-connector pattern
as test_fetch_all_leads.py — this tool's own logic (dedup consistency,
no run-lock) is what's under test, not GoogleSheetsConnector's
correctness. test_live_fetch_ad_hoc_sheet_against_a_real_connected_rep
is the one test that hits the real thing, auto-skipped until a rep has
a real completed OAuth connection with at least one granted file.
"""

import uuid

from sqlalchemy import select

from leadpilot import auth
from leadpilot.connectors.base import LeadRecord
from leadpilot.connectors.google_sheets import GoogleSheetsConnector
from leadpilot.models.dedup import LeadSourceRow
from leadpilot.models.leads import Lead
from leadpilot.models.rep_google_credential import RepGoogleCredential
from leadpilot.tools import fetch_ad_hoc_sheet
from leadpilot.tools.registry import load_all_tools

from fakes import FakeLeadSourceConnector

import pytest


def _make_rep(session) -> uuid.UUID:
    rep = auth.create_rep(session, email=f"{uuid.uuid4()}-fetch-ad-hoc-test@example.com", password="testpassword123")
    session.flush()
    return rep.rep_id


def _record(source_id, row_ref, name, phone=None, email=None, company=None, status=None):
    return LeadRecord(
        source_id=source_id, row_ref=row_ref, name=name, phone=phone, email=email, company=company,
        status=status, raw={"Name": name, "Phone": phone or "", "Email": email or "", "Status": status or ""},
    )


def test_registers_as_a_tool():
    tools = load_all_tools()
    assert "fetch_ad_hoc_sheet" in tools
    assert tools["fetch_ad_hoc_sheet"].handler is fetch_ad_hoc_sheet.run


def test_fetches_and_dedups_rows_from_one_source(db_session):
    rep_id = _make_rep(db_session)
    connector = FakeLeadSourceConnector({
        "ad_hoc_sheet": [
            _record("ad_hoc_sheet", "2", "John Doe", phone="555-1111"),
            _record("ad_hoc_sheet", "3", "Jane Smith", phone="555-2222"),
        ]
    })
    results = fetch_ad_hoc_sheet.run(db_session, rep_id, "ad_hoc_sheet", connector=connector)
    assert len(results) == 2
    assert {r["name"] for r in results} == {"John Doe", "Jane Smith"}


def test_dedups_against_a_lead_that_already_exists(db_session):
    """A lead already sitting in the leads table (e.g. from a prior
    fetch_all_leads run, or a previous ad hoc lookup) should still get
    matched by phone — the two tools share the same dedup logic.
    """
    rep_id = _make_rep(db_session)
    existing_lead = Lead(display_name="Jane Smith", primary_phone="555-2222")
    db_session.add(existing_lead)
    db_session.flush()

    connector = FakeLeadSourceConnector({
        "ad_hoc_sheet": [_record("ad_hoc_sheet", "9", "Jane Smith", phone="555-2222", status="New")],
    })
    results = fetch_ad_hoc_sheet.run(db_session, rep_id, "ad_hoc_sheet", connector=connector)
    assert results[0]["lead_id"] == str(existing_lead.lead_id)

    leads = db_session.execute(select(Lead)).scalars().all()
    assert len(leads) == 1  # no duplicate lead created


def test_rerun_is_idempotent(db_session):
    rep_id = _make_rep(db_session)
    connector = FakeLeadSourceConnector({
        "ad_hoc_sheet": [_record("ad_hoc_sheet", "2", "John Doe", phone="555-1111")],
    })
    fetch_ad_hoc_sheet.run(db_session, rep_id, "ad_hoc_sheet", connector=connector)
    fetch_ad_hoc_sheet.run(db_session, rep_id, "ad_hoc_sheet", connector=connector)

    rows = db_session.execute(select(LeadSourceRow)).scalars().all()
    assert len(rows) == 1


def test_raises_for_an_ungranted_source(db_session):
    """Doesn't trigger the Picker itself (that's a Step 3/caller
    concern) — just lets the connector's own validation surface
    naturally.
    """
    rep_id = _make_rep(db_session)
    connector = FakeLeadSourceConnector({"granted_sheet": [_record("granted_sheet", "2", "John Doe")]})
    with pytest.raises(ValueError):
        fetch_ad_hoc_sheet.run(db_session, rep_id, "not_granted_sheet", connector=connector)


def test_no_run_lock_involved(db_session):
    """Unlike fetch_all_leads, this is a one-off lookup — two calls for
    the same rep back to back must never block each other.
    """
    rep_id = _make_rep(db_session)
    connector = FakeLeadSourceConnector({"ad_hoc_sheet": [_record("ad_hoc_sheet", "2", "John Doe", phone="555-1111")]})
    fetch_ad_hoc_sheet.run(db_session, rep_id, "ad_hoc_sheet", connector=connector)
    # If this were gated by agent_run_locks, a second immediate call
    # would raise RunAlreadyInProgressError-style — it must not.
    results = fetch_ad_hoc_sheet.run(db_session, rep_id, "ad_hoc_sheet", connector=connector)
    assert len(results) == 1


def test_live_fetch_ad_hoc_sheet_against_a_real_connected_rep(db_session):
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
    results = fetch_ad_hoc_sheet.run(db_session, row.rep_id, source_id, connector=real_connector)
    assert len(results) > 0
    assert all(r["source_id"] == source_id for r in results)
