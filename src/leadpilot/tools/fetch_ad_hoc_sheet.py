"""fetch_ad_hoc_sheet — PRD v1.05 §3a/3e, Decision 028. Reads a single
Google Sheet the rep points LeadPilot at mid-session, outside the
routine hourly fetch_all_leads scan — "not a new interface method so
much as a different entry point into the same per-rep-authenticated
connector" (3e). Same dedup/upsert logic as fetch_all_leads
(leadpilot.lead_ingest), same per-row output shape, just for one
source_id instead of looping over the rep's full list_sources().

Does NOT trigger the Google Picker consent flow itself — that's a
Step 3/UI concern. If the rep hasn't already granted this specific
source_id, GoogleSheetsConnector._sheet_id_for raises a plain
ValueError ("has not granted access to source_id ..."); whatever calls
this tool (the agent loop, or Step 3's interface directly) is
responsible for catching that and prompting the rep through the
Picker, then retrying — see PRD 3a: "If the rep hasn't already granted
access to that specific sheet, this triggers the Picker so they can
grant it on the spot" describes the *caller's* responsibility, not
this tool's.

No run-lock — this is a one-off, rep-initiated lookup, not the
per-rep batch cycle agent_run_locks protects against overlapping runs
of. Two concurrent ad hoc lookups for the same rep against different
(or even the same) sheet aren't the failure mode that lock exists for.
"""

import uuid

from sqlalchemy.orm import Session

from leadpilot import lead_ingest
from leadpilot.connectors.base import LeadSourceConnector
from leadpilot.connectors.google_sheets import GoogleSheetsConnector
from leadpilot.tools.base import tool


@tool(
    name="fetch_ad_hoc_sheet",
    description=(
        "Reads a single Google Sheet the rep points LeadPilot at during a live session, for a "
        "one-off lookup outside the routine hourly scan. Dedups against existing canonical leads "
        "the same way fetch_all_leads does. Requires the rep to have already granted access to "
        "this specific sheet via the Google Picker — raises if they haven't."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "rep_id": {"type": "string", "description": "The requesting rep's UUID"},
            "source_id": {"type": "string", "description": "The Google Sheet file ID the rep wants read"},
        },
        "required": ["rep_id", "source_id"],
    },
)
def run(
    session: Session, rep_id: uuid.UUID, source_id: str, connector: LeadSourceConnector | None = None
) -> list[dict]:
    connector = connector or GoogleSheetsConnector(session, rep_id)

    try:
        results = []
        for record in connector.fetch_rows(source_id):
            lead_id = lead_ingest.upsert_lead_for_record(session, rep_id, source_id, record)
            results.append(lead_ingest.record_to_dict(source_id, lead_id, record))
        session.commit()
        return results
    except Exception:
        session.rollback()
        raise
