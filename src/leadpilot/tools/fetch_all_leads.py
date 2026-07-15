"""fetch_all_leads — PRD v1.05 §3a. Scans every Google Sheet the
requesting rep has personally connected, dedups rows against existing
canonical leads, and returns the compiled set. Never assumes access to
a sheet the rep hasn't granted (system prompt step 1, DATA ACCESS
GUARD) — enforced structurally, since GoogleSheetsConnector.list_sources()
only ever returns that rep's own granted files.

Manages its own commit boundaries around the run-lock acquire/release,
unlike gate.py/google_credentials.py (which leave committing to the
caller) — deliberate, not an inconsistency. The run-lock only works as
a real mutex if its acquisition is visible to other transactions
immediately; if the caller controlled the single outer commit instead,
two concurrent calls for the same rep could both see the lock as free
and both "acquire" it, defeating the whole point.

Dedup/upsert logic (Eval Case 2 — the same lead appearing on two
separate intake sheets must consolidate into one record) lives in
leadpilot.lead_ingest, shared with fetch_ad_hoc_sheet rather than
duplicated — both tools do the same "ingest a sheet's rows" work,
just with a different source_id loop around it.

Accepts an optional `connector` for testing against a fake
LeadSourceConnector implementation instead of a real Google API call —
real usage (the eventual Step 4 batch loop) never passes this,
defaulting to a real per-rep GoogleSheetsConnector.
"""

import uuid
from datetime import timedelta

from sqlalchemy.orm import Session

from leadpilot import lead_ingest, locks
from leadpilot.connectors.base import LeadSourceConnector
from leadpilot.connectors.google_sheets import GoogleSheetsConnector
from leadpilot.tools.base import tool

RUN_LOCK_STALE_AFTER = timedelta(hours=2)


class RunAlreadyInProgressError(Exception):
    """Raised instead of silently returning an empty result — an empty
    list could be mistaken for "this rep genuinely has zero leads,"
    which is a real, different state from "a run is already in flight
    for this rep, try again shortly."
    """


@tool(
    name="fetch_all_leads",
    description=(
        "Scans every Google Sheet the requesting rep has personally connected via Google OAuth, "
        "dedups rows against existing canonical leads, and returns the compiled set. Only ever "
        "reads sheets that rep has granted access to via the Google Picker — never a static "
        "admin-configured list."
    ),
    input_schema={
        "type": "object",
        "properties": {"rep_id": {"type": "string", "description": "The requesting rep's UUID"}},
        "required": ["rep_id"],
    },
)
def run(
    session: Session,
    rep_id: uuid.UUID,
    connector: LeadSourceConnector | None = None,
    manage_run_lock: bool = True,
) -> list[dict]:
    """`manage_run_lock=False` is for Step 4's batch runner
    (leadpilot.agent_run), which holds this rep's AgentRunLock for the
    *whole* agent run — acquiring again here would see the row locked
    and false-positive as an overlapping run. Any caller passing False
    must itself hold the rep's run lock for the duration; every other
    caller (the UI's sync button, direct invocation) leaves the
    default and gets the original self-managed mutex behavior.
    """
    connector = connector or GoogleSheetsConnector(session, rep_id)
    run_by = str(uuid.uuid4())

    if manage_run_lock:
        if not locks.acquire_run_lock(session, rep_id, run_by=run_by, stale_after=RUN_LOCK_STALE_AFTER):
            session.rollback()
            raise RunAlreadyInProgressError(f"A fetch_all_leads run is already in progress for rep {rep_id}")
        session.commit()  # lock acquisition must be visible to other transactions immediately

    try:
        results = []
        for source_id in connector.list_sources():
            for record in connector.fetch_rows(source_id):
                lead_id = lead_ingest.upsert_lead_for_record(session, source_id, record)
                results.append(lead_ingest.record_to_dict(source_id, lead_id, record))
        session.commit()
        return results
    except Exception:
        session.rollback()
        raise
    finally:
        if manage_run_lock:
            locks.release_run_lock(session, rep_id, run_by=run_by)
            session.commit()
