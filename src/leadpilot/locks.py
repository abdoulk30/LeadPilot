"""Atomic lock acquisition — real mechanisms backing the two tables in
models/run_lock.py. Both use a single atomic INSERT ... ON CONFLICT DO
UPDATE ... WHERE statement so the check-and-set is one round trip, not
a separate SELECT-then-UPDATE that a race could slip between.
"""

import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from leadpilot.models.run_lock import AgentRunLock, LeadActionLock


def try_acquire_lead_action_lock(session: Session, lead_id: uuid.UUID, cooldown: timedelta) -> bool:
    """Duplicate-contact prevention (security/threat-model.md). Call
    this before creating a new outreach draft for a lead. Returns True
    (and commits a fresh timestamp) only if there's no lock row yet, or
    the existing one is older than `cooldown` — otherwise returns False
    and leaves the existing timestamp untouched, meaning "don't draft
    another outreach action for this lead right now."

    The cooldown window itself is Step 2 business logic, not decided
    here — this is just the mechanism.
    """
    now = datetime.now(timezone.utc)
    stmt = (
        insert(LeadActionLock)
        .values(lead_id=lead_id, last_action_committed_at=now)
        .on_conflict_do_update(
            index_elements=[LeadActionLock.lead_id],
            set_={"last_action_committed_at": now},
            where=LeadActionLock.last_action_committed_at < (now - cooldown),
        )
        .returning(LeadActionLock.lead_id)
    )
    result = session.execute(stmt)
    return result.first() is not None


def acquire_run_lock(
    session: Session, rep_id: uuid.UUID, run_by: str, stale_after: timedelta
) -> bool:
    """Per-rep cron-job mutex (Decision 027/032 — reworked from a
    singleton). Returns True only if that rep's lock was free (no row
    yet, or `locked_at` is NULL) or was left stuck by a crashed run
    older than `stale_after` — that staleness fallback exists so a
    process that dies without calling release_run_lock doesn't block
    every future run for that rep forever. A different rep's lock row
    is untouched either way — this never blocks across reps.
    """
    now = datetime.now(timezone.utc)
    stmt = (
        insert(AgentRunLock)
        .values(rep_id=rep_id, locked_at=now, locked_by=run_by)
        .on_conflict_do_update(
            index_elements=[AgentRunLock.rep_id],
            set_={"locked_at": now, "locked_by": run_by},
            where=(AgentRunLock.locked_at.is_(None)) | (AgentRunLock.locked_at < (now - stale_after)),
        )
        .returning(AgentRunLock.rep_id)
    )
    result = session.execute(stmt)
    return result.first() is not None


def release_run_lock(session: Session, rep_id: uuid.UUID, run_by: str) -> bool:
    """Only the run that holds this rep's lock can release it — a
    stale/dead run's release call (if it somehow woke back up) can't
    clobber a newer run that has since taken the lock over via the
    staleness fallback.
    """
    result = session.execute(
        update(AgentRunLock)
        .where(AgentRunLock.rep_id == rep_id, AgentRunLock.locked_by == run_by)
        .values(locked_at=None, locked_by=None)
    )
    return result.rowcount == 1
