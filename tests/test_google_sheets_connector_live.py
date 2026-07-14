"""GoogleSheetsConnector tests — reworked for the per-rep OAuth model
(Decision 026), replacing the Step 1 service-account version.

Split into two sections:
1. Validation-only tests — real Postgres, but no live Google API call.
   These cover every error path that's decidable from stored data
   alone (list_sources, ungranted-source rejection) before the
   connector would ever try to reach Google.
2. Live tests — need a rep who has actually completed the real OAuth
   consent flow (GET /auth/google/connect) and granted at least one
   real sheet via the Picker. Auto-skipped until that exists, same
   pattern Step 1's version used for GOOGLE_SERVICE_ACCOUNT_KEY_PATH —
   see the docstring on skip_unless_live_rep_available below for how
   to make these actually run.

Per leadpilot-docs/testing/ci-strategy.md, neither section should run
in CI — the first because it hits real Postgres, the second because it
hits the real Google API. Both already match how this repo treats
real-dependency tests as local/manual, not CI.
"""

import uuid

import pytest
from sqlalchemy import select

from leadpilot import auth, google_credentials
from leadpilot.connectors.google_sheets import GoogleSheetsConnector, RepNotConnectedError
from leadpilot.models.rep_google_credential import RepGoogleCredential


def _make_rep(session) -> uuid.UUID:
    rep = auth.create_rep(session, email=f"{uuid.uuid4()}-rep@example.com", password="testpassword123")
    return rep.rep_id


# --- Section 1: validation-only, no live Google call ------------------


def test_list_sources_empty_for_unconnected_rep(db_session):
    rep_id = _make_rep(db_session)
    connector = GoogleSheetsConnector(db_session, rep_id)
    assert connector.list_sources() == []


def test_list_sources_returns_granted_files_for_connected_rep(db_session):
    rep_id = _make_rep(db_session)
    google_credentials.store_credential(db_session, rep_id, "fake-refresh-token")
    google_credentials.add_granted_file(db_session, rep_id, "fake-sheet-id-1")
    google_credentials.add_granted_file(db_session, rep_id, "fake-sheet-id-2")

    connector = GoogleSheetsConnector(db_session, rep_id)
    assert set(connector.list_sources()) == {"fake-sheet-id-1", "fake-sheet-id-2"}


def test_fetch_rows_for_ungranted_source_raises_without_network_call(db_session):
    rep_id = _make_rep(db_session)
    google_credentials.store_credential(db_session, rep_id, "fake-refresh-token")
    google_credentials.add_granted_file(db_session, rep_id, "granted-sheet")

    connector = GoogleSheetsConnector(db_session, rep_id)
    # "not-granted-sheet" was never granted — this must be rejected
    # before any attempt to mint an access token or call Google, which
    # a fake refresh token would fail loudly (and slowly) on.
    with pytest.raises(ValueError, match="has not granted access"):
        connector.fetch_rows("not-granted-sheet")


def test_stage_field_write_for_ungranted_source_raises_without_network_call(db_session):
    rep_id = _make_rep(db_session)
    google_credentials.store_credential(db_session, rep_id, "fake-refresh-token")

    connector = GoogleSheetsConnector(db_session, rep_id)
    with pytest.raises(ValueError, match="has not granted access"):
        connector.stage_field_write("not-granted-sheet", row_ref="2", field_name="status", value="Contacted")


def test_commit_field_write_for_ungranted_source_raises_without_network_call(db_session):
    rep_id = _make_rep(db_session)
    google_credentials.store_credential(db_session, rep_id, "fake-refresh-token")

    connector = GoogleSheetsConnector(db_session, rep_id)
    with pytest.raises(ValueError, match="has not granted access"):
        connector.commit_field_write(
            "not-granted-sheet", row_ref="2", field_name="status", value="Contacted", expected_current=None
        )


def test_client_raises_rep_not_connected_error_distinctly(db_session):
    """A rep who granted a file but then had their connection revoked
    (session._client() is only reached after _sheet_id_for's ungranted
    check passes) should get RepNotConnectedError specifically, not a
    generic ValueError — see the class docstring for why callers need
    to tell these apart.

    Both public entry points (_fetch_header_and_rows, commit_field_write)
    call _sheet_id_for() before _client(), and _sheet_id_for() already
    rejects any rep with no active credential (its granted-files list
    is empty in that case) — so in practice _client()'s own check is
    unreachable through the public API in a single synchronous call.
    It's still real, not dead code: two overlapping requests could
    race a revoke between _sheet_id_for()'s read and _client()'s under
    READ COMMITTED. That's awkward to simulate deterministically, so
    this exercises _client() directly instead of pretending the public
    API naturally reaches it here.
    """
    rep_id = _make_rep(db_session)
    # Deliberately no store_credential() call — this rep has no row in
    # rep_google_credentials at all, so get_refresh_token() returns None.
    connector = GoogleSheetsConnector(db_session, rep_id)
    with pytest.raises(RepNotConnectedError):
        connector._client()


# --- Section 2: live, needs a real completed OAuth connection ---------


def _find_live_test_rep(session) -> tuple[uuid.UUID, str] | None:
    """Looks for any rep with a real, active Google connection and at
    least one granted file — created by actually completing
    GET /auth/google/connect through a real browser, not by a test
    fixture. Returns (rep_id, one_granted_file_id) or None.
    """
    row = session.execute(
        select(RepGoogleCredential).where(RepGoogleCredential.revoked_at.is_(None))
    ).scalars().first()
    if row is None or not row.granted_file_ids:
        return None
    return row.rep_id, row.granted_file_ids[0]


def _skip_unless_live_rep_available(session):
    """Call at the top of each live test. To make these run: complete
    GET /auth/google/connect through a real browser (see the README/
    session notes on the manual walkthrough), then grant at least one
    file via the Google Picker once Step 3 builds that UI (or manually
    via google_credentials.add_granted_file for now). Until then these
    stay SKIPPED, not FAILED — same meaning as Step 1's version being
    skipped without GOOGLE_SERVICE_ACCOUNT_KEY_PATH.
    """
    result = _find_live_test_rep(session)
    if result is None:
        pytest.skip(
            "No rep has a real, active Google connection with at least one granted "
            "file yet — complete GET /auth/google/connect through a real browser first."
        )
    return result


def test_fetch_rows_returns_real_data(db_session):
    rep_id, sheet_id = _skip_unless_live_rep_available(db_session)
    connector = GoogleSheetsConnector(db_session, rep_id)

    records = connector.fetch_rows(sheet_id)
    assert len(records) > 0
    # Structural checks rather than hardcoded content — whichever real
    # sheet ends up granted when this is first run live, not
    # necessarily the exact Step 1 test sheet.
    for record in records:
        assert record.source_id == sheet_id
        assert record.row_ref.isdigit()


def test_stage_field_write_does_not_modify_the_sheet(db_session):
    rep_id, sheet_id = _skip_unless_live_rep_available(db_session)
    connector = GoogleSheetsConnector(db_session, rep_id)

    records = connector.fetch_rows(sheet_id)
    assert records, "live test sheet must have at least one row"
    target = records[0]

    before = {r.row_ref: r.status for r in connector.fetch_rows(sheet_id)}
    diff = connector.stage_field_write(sheet_id, row_ref=target.row_ref, field_name="status", value="TEST_PROBE")
    assert diff.current == before[target.row_ref]
    assert diff.proposed == "TEST_PROBE"

    after = {r.row_ref: r.status for r in connector.fetch_rows(sheet_id)}
    assert after == before, "stage_field_write must never write — sheet changed anyway"


def test_commit_field_write_actually_writes_and_is_reversible(db_session):
    rep_id, sheet_id = _skip_unless_live_rep_available(db_session)
    connector = GoogleSheetsConnector(db_session, rep_id)

    records = connector.fetch_rows(sheet_id)
    assert records, "live test sheet must have at least one row"
    target = records[0]
    original = target.status

    try:
        connector.commit_field_write(
            sheet_id, row_ref=target.row_ref, field_name="status", value="TEST_WRITE_PROBE",
            expected_current=original,
        )
        updated = next(r for r in connector.fetch_rows(sheet_id) if r.row_ref == target.row_ref)
        assert updated.status == "TEST_WRITE_PROBE"
    finally:
        connector.commit_field_write(
            sheet_id, row_ref=target.row_ref, field_name="status", value=original or "",
            expected_current="TEST_WRITE_PROBE",
        )
        restored = next(r for r in connector.fetch_rows(sheet_id) if r.row_ref == target.row_ref)
        assert restored.status == (original or "")


def test_commit_field_write_raises_stale_write_error_on_mismatch(db_session):
    """Decision 034 — the rep approved an edit based on a value that's
    since changed (simulated here by lying about expected_current).
    """
    rep_id, sheet_id = _skip_unless_live_rep_available(db_session)
    connector = GoogleSheetsConnector(db_session, rep_id)

    records = connector.fetch_rows(sheet_id)
    assert records, "live test sheet must have at least one row"
    target = records[0]

    from leadpilot.connectors.base import StaleWriteError

    with pytest.raises(StaleWriteError):
        connector.commit_field_write(
            sheet_id, row_ref=target.row_ref, field_name="status", value="SHOULD_NOT_WRITE",
            expected_current="__a_value_the_cell_definitely_does_not_currently_hold__",
        )
    # Must not have written anything.
    after = next(r for r in connector.fetch_rows(sheet_id) if r.row_ref == target.row_ref)
    assert after.status == target.status


def test_detect_changes_against_real_data(db_session):
    rep_id, sheet_id = _skip_unless_live_rep_available(db_session)
    connector = GoogleSheetsConnector(db_session, rep_id)
    records = connector.fetch_rows(sheet_id)
    assert len(records) >= 2, "live test sheet needs at least 2 rows to exercise new-vs-updated"

    from leadpilot.models.leads import Lead
    from leadpilot.models.dedup import LeadSourceRow

    seeded = records[:-1]  # leave at least one row unseeded, to show up as "new"
    seeded_refs = [r.row_ref for r in seeded]
    for i, record in enumerate(seeded):
        lead = Lead(display_name=record.name)
        db_session.add(lead)
        db_session.flush()
        stale_raw = dict(record.raw)
        if i == 0:
            stale_raw["Status"] = "__stale_value_from_a_previous_run__"
        db_session.add(
            LeadSourceRow(source_id=sheet_id, row_ref=record.row_ref, lead_id=lead.lead_id, raw_data=stale_raw)
        )
    db_session.flush()

    changes = connector.detect_changes(sheet_id, db_session)
    new_refs = {r.row_ref for r in changes.new_rows}
    updated_refs = {r.row_ref for r in changes.updated_rows}

    unseeded_ref = next(r.row_ref for r in records if r.row_ref not in seeded_refs)
    assert unseeded_ref in new_refs
    assert seeded_refs[0] in updated_refs
    assert seeded_refs[0] not in new_refs
