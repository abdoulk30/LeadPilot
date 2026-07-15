"""Step 4 — the hourly batch entrypoint the Render Cron Job runs:

    python -m leadpilot.agent_run

Iterates every rep with an active Google connection (Decision 027's
per-rep execution model) and runs the full agent loop once per rep,
holding that rep's AgentRunLock for the *whole* run — not just the
fetch — so the same rep's runs can never overlap while two different
reps' runs never contend. fetch_all_leads is told the lock is managed
externally (see its manage_run_lock docstring).

Failure isolation: one rep's failed run (bad credential, model
refusal, connector error) is recorded on their AgentRunReport row and
the loop moves to the next rep — a single rep can't take down the
whole batch. Exit code is nonzero only if *every* attempted run
failed, so Render's cron alerting fires on systemic breakage rather
than one rep's revoked token.
"""

import logging
import sys
import uuid
from datetime import datetime, timezone
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from leadpilot import agent_loop, gate, locks
from leadpilot.db import SessionLocal
from leadpilot.models.agent_run_report import AgentRunReport
from leadpilot.models.rep import Rep
from leadpilot.models.rep_google_credential import RepGoogleCredential
from leadpilot.config import settings

logger = logging.getLogger("leadpilot.agent_run")

# Mirrors fetch_all_leads.RUN_LOCK_STALE_AFTER: a crashed run stops
# blocking its rep after this long.
RUN_LOCK_STALE_AFTER = timedelta(hours=2)


def reps_with_active_google_connection(session: Session) -> list[Rep]:
    rows = session.execute(
        select(Rep)
        .join(RepGoogleCredential, RepGoogleCredential.rep_id == Rep.rep_id)
        .where(Rep.is_active.is_(True), RepGoogleCredential.revoked_at.is_(None))
    ).scalars().all()
    return list(rows)


def run_for_rep(session: Session, rep: Rep, anthropic_client=None) -> AgentRunReport:
    """One rep's full run, with the lock/report bookkeeping. Always
    returns the report row (committed) — status tells the story.
    """
    run_by = f"agent_run:{uuid.uuid4()}"
    report = AgentRunReport(rep_id=rep.rep_id, status="running", model=settings.anthropic_model)

    if not locks.acquire_run_lock(session, rep.rep_id, run_by=run_by, stale_after=RUN_LOCK_STALE_AFTER):
        session.rollback()
        report.status = "skipped_already_running"
        report.finished_at = datetime.now(timezone.utc)
        session.add(report)
        session.commit()
        logger.info("rep %s: run already in progress — skipped", rep.rep_id)
        return report

    session.add(report)
    session.commit()  # lock + running row visible immediately

    try:
        result = agent_loop.run_agent_for_rep(session, rep.rep_id, anthropic_client=anthropic_client)
        report.status = "succeeded"
        report.report = result.report
        report.iterations = result.iterations
        report.input_tokens = result.input_tokens
        report.output_tokens = result.output_tokens
    except agent_loop.AgentRunError as e:
        session.rollback()
        report.status = "refused" if "refusal" in str(e) else "failed"
        report.error = str(e)
        logger.error("rep %s: agent run failed: %s", rep.rep_id, e)
    except Exception as e:
        session.rollback()
        report.status = "failed"
        report.error = f"{type(e).__name__}: {e}"
        logger.exception("rep %s: agent run crashed", rep.rep_id)
    finally:
        report.finished_at = datetime.now(timezone.utc)
        locks.release_run_lock(session, rep.rep_id, run_by=run_by)
        session.commit()

    return report


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    session = SessionLocal()
    try:
        expired_count = gate.expire_stale_drafts(session)
        session.commit()
        if expired_count:
            logger.info("Expired %d stale draft(s) past gate.DEFAULT_STALE_AFTER", expired_count)

        reps = reps_with_active_google_connection(session)
        if not reps:
            logger.info("No reps with an active Google connection — nothing to run.")
            return 0

        outcomes = []
        for rep in reps:
            logger.info("Starting agent run for rep %s (%s)", rep.rep_id, rep.email)
            report = run_for_rep(session, rep)
            outcomes.append(report.status)
            logger.info("rep %s: %s", rep.rep_id, report.status)

        attempted = [s for s in outcomes if s != "skipped_already_running"]
        if attempted and all(s in ("failed", "refused") for s in attempted):
            logger.error("Every attempted run failed — signaling cron failure.")
            return 1
        return 0
    finally:
        session.close()


if __name__ == "__main__":
    sys.exit(main())
