"""testing/eval-suite.md Case 5 — Destructive action confirmation.

The rep edits a lead's status field in the interface, then abandons
without confirming: update_lead_sheet must never be called with real
effect, and the source spreadsheet must be unchanged. run() (staging)
always shows a diff and never writes; only an explicit approval —
which never happens in this scenario — lets execute() write anything.
"""

import uuid

from leadpilot import auth
from leadpilot.connectors.base import LeadRecord
from leadpilot.models.contact_history import ContactHistory, Stage
from leadpilot.models.leads import Lead
from leadpilot.tools import update_lead_sheet

from fakes import FakeLeadSourceConnector


def _make_rep(session) -> uuid.UUID:
    rep = auth.create_rep(session, email=f"{uuid.uuid4()}-eval-case-5@example.com", password="testpassword123")
    session.flush()
    return rep.rep_id


def _make_lead(session) -> uuid.UUID:
    lead = Lead(display_name="Test Lead")
    session.add(lead)
    session.flush()
    return lead.lead_id


def test_case_5_destructive_action_confirmation(db_session):
    rep_id = _make_rep(db_session)
    lead_id = _make_lead(db_session)
    connector = FakeLeadSourceConnector({
        "sheet_1": [
            LeadRecord(
                source_id="sheet_1", row_ref="2", name="Test Lead", phone=None, email=None,
                company=None, status="Uncontacted", raw={"Status": "Uncontacted"},
            ),
        ],
    })

    # The rep edits the field in the interface — this is exactly
    # run()'s job: compute and show the diff, write nothing.
    staged = update_lead_sheet.run(
        db_session, rep_id, lead_id, "sheet_1", "2", "status", "Contacted", connector=connector
    )

    # The interface shows a current-vs-proposed diff before any write.
    assert staged["current"] == "Uncontacted"
    assert staged["proposed"] == "Contacted"
    assert staged["status"] == "awaiting_rep_approval"

    # The rep closes the tab without confirming — no approve, no execute.
    event = db_session.get(ContactHistory, uuid.UUID(staged["event_id"]))
    assert event.stage == Stage.AWAITING_REP_APPROVAL

    # update_lead_sheet.execute() is never called — no approval token
    # was ever minted, so nothing downstream could even attempt it.
    assert connector._writes == []

    # The source spreadsheet itself is unchanged.
    row = connector._rows_by_source["sheet_1"][0]
    assert row.status == "Uncontacted"
