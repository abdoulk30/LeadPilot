"""Real integration tests against the actual live Google Sheet set up
for Step 1 — not mocks. This is the "implemented against the actual
API at least once, with real credentials" requirement from
leadpilot-docs/testing/definition-of-done.md.

Per leadpilot-docs/testing/ci-strategy.md ("Never let CI make real
Slack posts or real Google API calls"), these must NOT run in the
automated eval-suite/CI path once that exists — they're skipped
automatically if GOOGLE_SERVICE_ACCOUNT_KEY_PATH isn't configured
(e.g. on Marc's machine or in CI), so they only run where someone has
deliberately set up real credentials, matching how this repo already
treats real-API tests as a manual/local verification step.
"""

import os

import pytest

from leadpilot.config import settings
from leadpilot.connectors.google_sheets import GoogleSheetsConnector
from leadpilot.models.dedup import LeadSourceRow

pytestmark = pytest.mark.skipif(
    not settings.google_service_account_key_path or not os.path.exists(settings.google_service_account_key_path),
    reason="No GOOGLE_SERVICE_ACCOUNT_KEY_PATH configured — skipping real Google Sheets API tests",
)


@pytest.fixture()
def connector():
    return GoogleSheetsConnector()


def test_list_sources(connector):
    assert connector.list_sources() == ["test_sheet"]


def test_fetch_rows_returns_real_data(connector):
    records = connector.fetch_rows("test_sheet")
    assert len(records) == 5

    john = next(r for r in records if r.name == "John Doe")
    assert john.phone == "555-201-4488"
    assert john.email == "john.doe@example.com"
    assert john.company == "Doe Roofing"
    assert john.status == "Uncontacted"
    assert john.raw["Source"] == "Inbound Sheet A"

    # Confirms dedup-relevant reality from Eval Case 2: two leads can
    # share a source tag without being the same lead.
    same_source = [r for r in records if r.raw["Source"] == "Inbound Sheet A"]
    assert len(same_source) == 2


def test_stage_field_write_does_not_modify_the_sheet(connector):
    before = {r.row_ref: r.status for r in connector.fetch_rows("test_sheet")}

    diff = connector.stage_field_write("test_sheet", row_ref="2", field_name="status", value="Contacted")
    assert diff.current == before["2"]
    assert diff.proposed == "Contacted"

    after = {r.row_ref: r.status for r in connector.fetch_rows("test_sheet")}
    assert after == before, "stage_field_write must never write — sheet changed anyway"


def test_commit_field_write_actually_writes_and_is_reversible(connector):
    original = next(r for r in connector.fetch_rows("test_sheet") if r.row_ref == "2").status
    try:
        connector.commit_field_write("test_sheet", row_ref="2", field_name="status", value="TEST_WRITE_PROBE")
        updated = next(r for r in connector.fetch_rows("test_sheet") if r.row_ref == "2")
        assert updated.status == "TEST_WRITE_PROBE"
    finally:
        # Leave the shared test sheet exactly as it was found.
        connector.commit_field_write("test_sheet", row_ref="2", field_name="status", value=original)
        restored = next(r for r in connector.fetch_rows("test_sheet") if r.row_ref == "2")
        assert restored.status == original


def test_detect_changes_against_real_data(connector, db_session):
    records = connector.fetch_rows("test_sheet")

    # Simulate "last run saw 4 of the 5 rows, and one of those 4 has
    # since changed" by seeding lead_source_rows directly, bypassing
    # the connector — this is what a previous fetch_all_leads run
    # would have persisted.
    from leadpilot.models.leads import Lead

    seeded_refs = [r.row_ref for r in records[:4]]
    for record in records[:4]:
        lead = Lead(display_name=record.name)
        db_session.add(lead)
        db_session.flush()
        stale_raw = dict(record.raw)
        if record.row_ref == seeded_refs[0]:
            # Make this one stale on purpose so it shows up as "updated".
            stale_raw["Status"] = "__stale_value_from_a_previous_run__"
        db_session.add(
            LeadSourceRow(
                source_id="test_sheet",
                row_ref=record.row_ref,
                lead_id=lead.lead_id,
                raw_data=stale_raw,
            )
        )
    db_session.flush()

    changes = connector.detect_changes("test_sheet", db_session)

    new_refs = {r.row_ref for r in changes.new_rows}
    updated_refs = {r.row_ref for r in changes.updated_rows}

    unseeded_ref = next(r.row_ref for r in records if r.row_ref not in seeded_refs)
    assert unseeded_ref in new_refs
    assert seeded_refs[0] in updated_refs
    assert seeded_refs[0] not in new_refs
