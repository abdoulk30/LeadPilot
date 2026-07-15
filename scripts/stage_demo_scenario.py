"""Stage the 20-contact demo scenario (Marc, 2026-07-15).

Reshapes the REAL granted Google Sheets and LeadPilot's DB into a
demo-ready world. Explicitly authorized by Marc for his synthetic
demo sheets (555 numbers, example.com emails) — do not point this at
production business data.

What it does, in order:
 1. Sheets: trims each granted sheet to a few data rows (8/5/4/3 → 20
    contacts total), preserving legend + header rows; mirrors one
    sheet-1 contact's phone/email into the first Business Name sheet
    row (live dedup + company-enrichment demo); ensures a Status
    column exists everywhere; writes a spread of v1 statuses.
 2. DB: wipes all lead data (leads/history/source rows/reports),
    re-syncs from the trimmed sheets via the real connector.
 3. Story: fabricates varied contact history (Rank 1 answered calls,
    Rank 3 follow-ups, an unlogged call for the amber strip, a second
    rep for actor variety) and stages real drafts through the real
    tools (texts, an email, an urgent Slack handoff, a sheet-edit
    diff) — everything awaiting approval, nothing executed.

    python scripts/stage_demo_scenario.py            # everything
    python scripts/stage_demo_scenario.py --story    # re-stage history/
        # drafts only (e.g. after a Reset wiped them) — sheets untouched
"""

import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from sqlalchemy import select  # noqa: E402

from leadpilot import auth, google_credentials  # noqa: E402
from leadpilot.config import settings  # noqa: E402
from leadpilot.connectors.google_sheets import (  # noqa: E402
    GoogleSheetsConnector,
    _column_letter,
    _detect_header_index,
    _resolve_header,
)
from leadpilot.db import SessionLocal  # noqa: E402
from leadpilot.models.agent_run_report import AgentRunReport  # noqa: E402
from leadpilot.models.contact_history import Channel, ContactHistory, Outcome, Stage, Tool  # noqa: E402
from leadpilot.models.dedup import LeadSourceRow  # noqa: E402
from leadpilot.models.leads import LEAD_STATUS_OPTIONS, Lead  # noqa: E402
from leadpilot.models.rep import Rep  # noqa: E402
from leadpilot.models.run_lock import AgentRunLock, LeadActionLock, SheetCellLock  # noqa: E402
from leadpilot.tools import (  # noqa: E402
    dispatch_slack_handoff,
    fetch_all_leads,
    initiate_lead_call,
    send_lead_email,
    send_lead_text,
    update_lead_sheet,
)

# data rows to keep per granted sheet, in grant order
KEEP_PER_SHEET = [8, 5, 4, 3]


def sheets_api(connector):
    return connector._client().spreadsheets()


def first_tab_gid(api, spreadsheet_id):
    meta = api.get(spreadsheetId=spreadsheet_id, fields="sheets(properties(sheetId))").execute()
    return meta["sheets"][0]["properties"]["sheetId"]


def trim_sheet(api, spreadsheet_id, keep_data_rows):
    """Keeps everything up to and including the header row plus
    `keep_data_rows` data rows (legend rows below the header count as
    data and get skipped at ingest anyway — keep one extra row of
    slack for them on sheet 1, handled by the caller)."""
    values = api.values().get(spreadsheetId=spreadsheet_id, range="A1:ZZ2000").execute().get("values", [])
    header_idx = _detect_header_index(values)
    last_keep = header_idx + keep_data_rows  # 0-based index of last kept row
    total = len(values)
    if total - 1 <= last_keep:
        return 0
    gid = first_tab_gid(api, spreadsheet_id)
    api.batchUpdate(
        spreadsheetId=spreadsheet_id,
        body={"requests": [{
            "deleteDimension": {
                "range": {"sheetId": gid, "dimension": "ROWS",
                          "startIndex": last_keep + 1, "endIndex": total}
            }
        }]},
    ).execute()
    return total - (last_keep + 1)


def write_cell(api, spreadsheet_id, a1, value):
    api.values().update(
        spreadsheetId=spreadsheet_id, range=a1, valueInputOption="RAW",
        body={"values": [[value]]},
    ).execute()


def main():
    story_only = "--story" in sys.argv
    session = SessionLocal()
    rep = session.execute(select(Rep).where(Rep.email == "demo@leadpilot.dev")).scalar_one()
    granted = google_credentials.granted_file_ids(session, rep.rep_id)
    if not granted:
        print("No granted sheets — connect + grant first.")
        return 1
    print(f"granted sheets: {len(granted)}")

    connector = GoogleSheetsConnector(session, rep.rep_id)
    api = sheets_api(connector)

    if story_only:
        # Clear any leftover history/drafts, keep leads as they are.
        for model in (ContactHistory, LeadActionLock, SheetCellLock, AgentRunReport):
            session.query(model).delete()
        session.commit()
        leads = session.execute(select(Lead)).scalars().all()
        if not leads:
            print("No leads in the DB — run without --story first (or Sync sheets).")
            return 1
        print(f"story-only: {len(leads)} existing leads")
        return stage_story(session, rep, connector, leads)

    # ---- Phase 1: sheet surgery -----------------------------------------
    mirror_contact = None  # (phone, email) of a sheet-1 keeper
    for i, source_id in enumerate(granted):
        keep = KEEP_PER_SHEET[i] if i < len(KEEP_PER_SHEET) else 3
        # sheet 1 carries its legend as a data row below the header —
        # keep one extra raw row so trimming doesn't eat a real contact.
        extra = 1 if i == 0 else 0
        removed = trim_sheet(api, source_id, keep + extra)
        print(f"sheet {i + 1}: trimmed {removed} rows (kept ~{keep} contacts)")

        # Status column everywhere
        header_name = connector.add_status_column(source_id)

        # Re-read post-trim to place statuses + find the mirror contact
        values = api.values().get(spreadsheetId=source_id, range="A1:ZZ100").execute().get("values", [])
        header_idx = _detect_header_index(values)
        header = values[header_idx]
        status_col = _column_letter(header.index(_resolve_header(header, "status")))
        phone_header = _resolve_header(header, "phone")
        email_header = _resolve_header(header, "email")

        statuses = list(LEAD_STATUS_OPTIONS) + ["", ""]  # blanks are valid too
        data_rows = list(enumerate(values[header_idx + 1:], start=header_idx + 2))
        wrote = 0
        for j, (rownum, row) in enumerate(data_rows):
            padded = row + [""] * (len(header) - len(row))
            rowdict = dict(zip(header, padded))
            # skip legend/annotation rows (no contact data)
            if not (rowdict.get(phone_header) or rowdict.get(email_header)):
                continue
            write_cell(api, source_id, f"{status_col}{rownum}", statuses[wrote % len(statuses)])
            wrote += 1
            if i == 0 and mirror_contact is None:
                mirror_contact = (rowdict.get(phone_header), rowdict.get(email_header))

        # dedup demo: first data row of sheet 2 becomes the same person
        # as sheet 1's first keeper (same phone+email, keeps its
        # Business Name → enrichment fills the company).
        if i == 1 and mirror_contact:
            for rownum, row in data_rows:
                padded = row + [""] * (len(header) - len(row))
                rowdict = dict(zip(header, padded))
                if rowdict.get(phone_header) or rowdict.get(email_header):
                    if phone_header:
                        write_cell(api, source_id, f"{_column_letter(header.index(phone_header))}{rownum}", mirror_contact[0] or "")
                    if email_header:
                        write_cell(api, source_id, f"{_column_letter(header.index(email_header))}{rownum}", mirror_contact[1] or "")
                    print(f"sheet 2 row {rownum}: mirrored sheet-1 contact for the dedup demo")
                    break
        print(f"sheet {i + 1}: statuses written to {wrote} rows (column {status_col}, header {header_name!r})")

    # ---- Phase 2: rebuild LeadPilot's world -------------------------------
    for model in (ContactHistory, LeadActionLock, SheetCellLock, LeadSourceRow, AgentRunReport):
        session.query(model).delete()
    session.query(Lead).delete()
    session.query(AgentRunLock).delete()
    session.commit()
    print("wiped lead data")

    rows = fetch_all_leads.run(session, rep.rep_id, connector=connector)
    leads = session.execute(select(Lead)).scalars().all()
    print(f"re-synced: {len(rows)} rows -> {len(leads)} canonical leads")

    return stage_story(session, rep, connector, leads)


def stage_story(session, rep, connector, leads):
    # ---- Phase 3: history + staged drafts ---------------------------------
    other = session.execute(select(Rep).where(Rep.email == "abdoul-demo@leadpilot.dev")).scalar_one_or_none()
    if other is None:
        other = auth.create_rep(session, email="abdoul-demo@leadpilot.dev",
                                password="demopassword123", display_name="Abdoul Demo")
        session.commit()

    if not settings.slack_handoff_channel_ids:
        settings.slack_handoff_channel_ids = "C0DEMO001,C0DEMO002,C0DEMO003"

    now = datetime.now(timezone.utc)

    def history(lead, *, tool, channel, stage, outcome=None, rep_id=None, note=None, content=None, hours_ago=0.0):
        session.add(ContactHistory(
            lead_id=lead.lead_id, tool=tool, channel=channel, stage=stage, outcome=outcome,
            rep_id=rep_id, note=note, content_ref=content,
            timestamp=now - timedelta(hours=hours_ago),
        ))
        session.commit()

    leads.sort(key=lambda l: ((l.company or l.display_name) or "").lower())
    L = leads  # shorthand

    # Rank 1: answered call 3h ago (you) + follow-up text already drafted
    history(L[0], tool=Tool.INITIATE_LEAD_CALL, channel=Channel.CALL, stage=Stage.EXECUTED,
            outcome=Outcome.ANSWERED, rep_id=rep.rep_id,
            note="Very interested — wants terms today", content=L[0].primary_phone, hours_ago=3)
    send_lead_text.send_lead_text(session, lead_id=L[0].lead_id, message=(
        f"Hi {(L[0].display_name or '').split(' ')[0]}, great speaking earlier! "
        "Sending over the term overview now — reply here with any questions and "
        "I'll walk you through the next step."
    ))
    session.commit()

    # Rank 1 by the other rep (actor variety in the timeline)
    history(L[1], tool=Tool.INITIATE_LEAD_CALL, channel=Channel.CALL, stage=Stage.EXECUTED,
            outcome=Outcome.ANSWERED, rep_id=other.rep_id,
            note="Asked for a callback tomorrow AM", content=L[1].primary_phone, hours_ago=20)

    # Unlogged call → the amber strip (26h ago, outcome pending)
    history(L[2], tool=Tool.INITIATE_LEAD_CALL, channel=Channel.CALL, stage=Stage.EXECUTED,
            outcome=Outcome.PENDING, rep_id=rep.rep_id, content=L[2].primary_phone, hours_ago=26)

    # Rank 3: no-answer yesterday + old delivered text + email draft ready
    history(L[3], tool=Tool.SEND_LEAD_TEXT, channel=Channel.TEXT, stage=Stage.EXECUTED,
            outcome=Outcome.DELIVERED, rep_id=rep.rep_id,
            content="Checking in — still interested in funding this quarter?", hours_ago=76)
    history(L[3], tool=Tool.INITIATE_LEAD_CALL, channel=Channel.CALL, stage=Stage.EXECUTED,
            outcome=Outcome.NO_ANSWER, rep_id=rep.rep_id, content=L[3].primary_phone, hours_ago=25)
    if L[3].primary_email:
        send_lead_email.send_lead_email(
            session, lead_id=L[3].lead_id,
            subject="Following up on your funding application",
            body=("Hi,\n\nTried reaching you by phone — following up here instead. "
                  "Your application is still active and we can move quickly once "
                  "you're ready. What's the best time for a short call?\n\nBest,\nMarc"),
        )
        session.commit()

    # Rank 3 cadence: single old text
    history(L[4], tool=Tool.SEND_LEAD_TEXT, channel=Channel.TEXT, stage=Stage.EXECUTED,
            outcome=Outcome.DELIVERED, rep_id=other.rep_id,
            content="Hi — your prequal is approved, want to continue?", hours_ago=100)

    # Urgent back-office handoff (sorts to top, amber badge)
    dispatch_slack_handoff.dispatch_slack_handoff(
        session, lead_id=L[5].lead_id, message_type="urgent_callback_request",
        message=(f"URGENT: {L[5].display_name} ({L[5].company or 'no company'}) needs a "
                 "back-office callback about rate options before 5pm today."),
    )
    session.commit()

    # Timeline variety: a rejected draft in the past
    history(L[6], tool=Tool.SEND_LEAD_TEXT, channel=Channel.TEXT, stage=Stage.REJECTED,
            rep_id=rep.rep_id, content="(draft the rep rejected last week)", hours_ago=120)

    # Live sheet-write demo: staged status diff on the Rank-1 lead's row
    src_row = session.execute(
        select(LeadSourceRow).where(LeadSourceRow.lead_id == L[0].lead_id)
    ).scalars().first()
    if src_row:
        update_lead_sheet.run(
            session, rep.rep_id, L[0].lead_id, src_row.source_id, src_row.row_ref,
            "status", "Interested", connector=connector,
        )

    pending = session.query(ContactHistory).filter(
        ContactHistory.stage == Stage.AWAITING_REP_APPROVAL
    ).count()
    print(f"\nDemo world ready: {len(leads)} leads, {pending} drafts awaiting approval.")
    print("Beats: R1 answered calls (x2, two reps), amber unlogged call, R3 follow-ups,")
    print("urgent handoff, staged sheet diff (approve it live — writes 'Interested' to the sheet),")
    print("cross-sheet dedup with company enrichment on the mirrored contact.")
    print("\nNOTE: don't approve the Slack handoff live unless SLACK_HANDOFF_CHANNEL_IDS")
    print("is set to a real channel — placeholders were used for staging only.")
    session.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
