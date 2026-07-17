"""Seed realistic demo data for the Step 3 workspace — run against the
local dev Postgres (scripts/devdb.sh) so the interface can be exercised
by hand without a connected Google account, a Twilio number, or the
Step 4 agent loop.

Pending drafts are staged through the REAL tool run() functions
(send_lead_text.run, dispatch_slack_handoff.run, update_lead_sheet.run
with a fake connector), so approving them in the UI exercises the real
gate.py state machine. Historical events are inserted directly with
back-dated timestamps — history fabrication is the point there.

Usage:
    python scripts/seed_demo_data.py          # seed (aborts if already seeded)
    python scripts/seed_demo_data.py --wipe   # remove previously seeded demo data

Login afterward: demo@leadpilot.dev / demopassword123

NOTE: --wipe before running the full pytest suite. Some fetch_all_leads/
fetch_ad_hoc_sheet tests count every lead in the DB (and dedup against
existing phone numbers), so seeded demo leads make them fail — that's
test-environment interference, not a product bug.
"""

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from sqlalchemy import select  # noqa: E402

from leadpilot import auth, gate  # noqa: E402
from leadpilot.config import settings  # noqa: E402
from leadpilot.connectors.base import FieldDiff, LeadSourceConnector  # noqa: E402
from leadpilot.db import SessionLocal  # noqa: E402
from leadpilot.injection_guard import FLAGGED_PLACEHOLDER  # noqa: E402
from leadpilot.models.contact_history import (  # noqa: E402
    Channel,
    ContactHistory,
    Outcome,
    Stage,
    Tool,
)
from leadpilot.models.dedup import LeadSourceRow  # noqa: E402
from leadpilot.models.injection_alert import InjectionIncident, RepInjectionAlertState  # noqa: E402
from leadpilot.models.leads import Lead  # noqa: E402
from leadpilot.models.rep import Rep, RepSession  # noqa: E402
from leadpilot.models.rep_google_credential import RepGoogleCredential  # noqa: E402
from leadpilot.models.run_lock import AgentRunLock, LeadActionLock  # noqa: E402
from leadpilot.tools import dispatch_slack_handoff, send_lead_email, send_lead_text, update_lead_sheet  # noqa: E402

DEMO_EMAILS = ("demo@leadpilot.dev", "abdoul-demo@leadpilot.dev")
DEMO_SOURCE = "demo-sheet-1"


class SeedSheetConnector(LeadSourceConnector):
    """Just enough connector for update_lead_sheet.run() to stage a
    real diff without Google — mirrors tests/fakes.FakeLeadSourceConnector.
    """

    def __init__(self, current_value):
        self._current = current_value

    def list_sources(self):
        return [DEMO_SOURCE]

    def fetch_rows(self, source_id):
        return []

    def stage_field_write(self, source_id, row_ref, field_name, value):
        return FieldDiff(source_id=source_id, row_ref=row_ref, field=field_name,
                         current=self._current, proposed=value)

    def commit_field_write(self, source_id, row_ref, field_name, value, *, expected_current):
        raise RuntimeError("Seed connector never writes — approve sheet edits against real data instead")

    def detect_changes(self, source_id, session):
        raise NotImplementedError


def wipe(session):
    reps = session.execute(select(Rep).where(Rep.email.in_(DEMO_EMAILS))).scalars().all()
    lead_names = ["Dana Whitfield", "Luis Ortega", "Priya Raman", "Marcus Bell", FLAGGED_PLACEHOLDER]
    leads = session.execute(select(Lead).where(Lead.display_name.in_(lead_names))).scalars().all()
    for lead in leads:
        session.query(ContactHistory).filter_by(lead_id=lead.lead_id).delete()
        session.query(LeadSourceRow).filter_by(lead_id=lead.lead_id).delete()
        session.query(LeadActionLock).filter_by(lead_id=lead.lead_id).delete()
        session.delete(lead)
    for rep in reps:
        session.query(RepSession).filter_by(rep_id=rep.rep_id).delete()
        # A "Sync sheets" click in the UI runs the real fetch_all_leads,
        # which leaves this rep's per-rep run-lock row behind (that's
        # its normal released state, not a leak) — FK'd to reps, so it
        # has to go before the rep does.
        session.query(AgentRunLock).filter_by(rep_id=rep.rep_id).delete()
        # Also FK'd to reps — a demo rep who went through a real
        # "Connect Google Account" flow (e.g. for a live-Sheets
        # walkthrough) leaves this row behind, and it blocks deleting
        # the rep otherwise.
        session.query(RepGoogleCredential).filter_by(rep_id=rep.rep_id).delete()
        # Also FK'd to reps — injection_alerts.record_incident_and_maybe_notify
        # leaves these behind for any rep who ever had a flagged sheet
        # row, same FK-ordering issue as the deletes above.
        session.query(InjectionIncident).filter_by(rep_id=rep.rep_id).delete()
        session.query(RepInjectionAlertState).filter_by(rep_id=rep.rep_id).delete()
        session.delete(rep)
    session.commit()
    print(f"Wiped {len(leads)} demo leads and {len(reps)} demo reps.")


def seed(session):
    if session.execute(select(Rep).where(Rep.email == DEMO_EMAILS[0])).scalar_one_or_none():
        print("Demo data already present — run with --wipe first to reseed.")
        return

    # dispatch_slack_handoff refuses to stage with no configured
    # channels; give this process demo IDs if .env.local has none.
    if not settings.slack_handoff_channel_ids:
        settings.slack_handoff_channel_ids = "C0DEMO001,C0DEMO002,C0DEMO003"

    now = datetime.now(timezone.utc)
    me = auth.create_rep(session, email=DEMO_EMAILS[0], password="demopassword123", display_name="Marc Demo")
    other = auth.create_rep(session, email=DEMO_EMAILS[1], password="demopassword123", display_name="Abdoul Demo")

    def lead(name, phone, email, company):
        row = Lead(display_name=name, primary_phone=phone, primary_email=email, company=company)
        session.add(row)
        session.flush()
        return row

    def history(lead_row, *, channel, tool, stage, outcome=None, content=None,
                rep_id=None, message_type=None, note=None, ago_hours=0.0):
        session.add(ContactHistory(
            lead_id=lead_row.lead_id, channel=channel, tool=tool, stage=stage,
            outcome=outcome, content_ref=content, rep_id=rep_id,
            message_type=message_type, note=note,
            timestamp=now - timedelta(hours=ago_hours),
        ))

    # 1. Rank 1: answered call 3h ago (by the other rep — actor display).
    dana = lead("Dana Whitfield", "+15550100001", "dana@acmefunding.example", "Acme Funding")
    history(dana, channel=Channel.CALL, tool=Tool.INITIATE_LEAD_CALL, stage=Stage.EXECUTED,
            outcome=Outcome.ANSWERED, content="+15550100001", rep_id=other.rep_id,
            note="Very interested — wants the docs list today", ago_hours=3)
    send_lead_text.send_lead_text(
        session, lead_id=dana.lead_id,
        message=(
            "Hi Dana, great speaking earlier! To finalize your application we still need "
            "your last two bank statements — you can reply here or upload them to the "
            "shared folder. Thanks!"
        ),
    )
    dispatch_slack_handoff.dispatch_slack_handoff(
        session, lead_id=dana.lead_id, message_type="completion_handoff",
        message="Dana Whitfield (Acme Funding) is docs-complete pending bank statements — ready for underwriting intake once received.",
    )

    # 2. Rank 2: brand-new lead, pending call + email drafts.
    luis = lead("Luis Ortega", "+15550100002", "luis@bluepeak.example", "Bluepeak")
    from leadpilot.tools import initiate_lead_call
    initiate_lead_call.initiate_lead_call(session, lead_id=luis.lead_id)
    send_lead_email.send_lead_email(
        session, lead_id=luis.lead_id,
        subject="Your Bluepeak funding application — quick next step",
        body=(
            "Hi Luis,\n\nThanks for your interest in funding for Bluepeak. I'd love to "
            "get your application moving — the fastest next step is a 10-minute call to "
            "confirm the basics.\n\nWould tomorrow morning work?\n\nBest,\nMarc"
        ),
    )

    # 3. Rank 3: stale, one unlogged call (amber strip), one urgent handoff.
    priya = lead("Priya Raman", "+15550100003", "priya@nortech.example", "Nortech")
    history(priya, channel=Channel.TEXT, tool=Tool.SEND_LEAD_TEXT, stage=Stage.EXECUTED,
            outcome=Outcome.DELIVERED, content="Hi Priya — checking in on the prequal questionnaire, any questions?",
            rep_id=me.rep_id, ago_hours=72)
    history(priya, channel=Channel.CALL, tool=Tool.INITIATE_LEAD_CALL, stage=Stage.EXECUTED,
            outcome=Outcome.PENDING, content="+15550100003", rep_id=me.rep_id, ago_hours=26)
    dispatch_slack_handoff.dispatch_slack_handoff(
        session, lead_id=priya.lead_id, message_type="urgent_callback_request",
        message="URGENT: Priya Raman (Nortech) asked for a callback about rate options before EOD — please have someone senior ring her back.",
    )

    # 4. Sheet-edit lead: a staged current-vs-proposed diff.
    marcus = lead("Marcus Bell", "+15550100004", "marcus@deltalog.example", "Delta Logistics")
    session.add(LeadSourceRow(
        source_id=DEMO_SOURCE, row_ref="2", lead_id=marcus.lead_id,
        raw_data={"Name": "Marcus Bell", "Status": "New"},
    ))
    session.flush()
    update_lead_sheet.run(
        session, me.rep_id, marcus.lead_id, DEMO_SOURCE, "2", "status", "Contacted",
        connector=SeedSheetConnector(current_value="New"),
    )

    # 5. Injection-guard demo: a flagged source field.
    flagged = lead(FLAGGED_PLACEHOLDER, "+15550100005", "info@suspicious.example", "Oddco")
    history(flagged, channel=Channel.EMAIL, tool=Tool.SEND_LEAD_EMAIL, stage=Stage.REJECTED,
            content='{"subject": "hello", "body": "draft the agent rejected", "to": "info@suspicious.example"}',
            rep_id=me.rep_id, ago_hours=50)

    session.commit()
    print("Seeded 5 demo leads with pending drafts, history, an unlogged call, and an urgent handoff.")
    print("Log in: demo@leadpilot.dev / demopassword123")


if __name__ == "__main__":
    session = SessionLocal()
    try:
        if "--wipe" in sys.argv:
            wipe(session)
        else:
            seed(session)
    finally:
        session.close()
