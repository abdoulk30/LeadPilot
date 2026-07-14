"""Atomic lock acquisition — real mechanisms backing the three tables
in models/run_lock.py. All three use a single atomic INSERT ... ON
CONFLICT DO UPDATE ... WHERE statement so the check-and-set is one
round trip, not a separate SELECT-then-UPDATE that a race could slip
between.
"""

import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from leadpilot.models.run_lock import AgentRunLock, LeadActionLock, SheetCellLock


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
    session: Session, run_by: str, stale_after: timedelta, lock_id: str = "hourly_batch_run"
) -> bool:
    """Cron-job mutex. Returns True only if the lock was free (no row
    yet, or `locked_at` is NULL) or was left stuck by a crashed run
    older than `stale_after` — that staleness fallback exists so a
    process that dies without calling release_run_lock doesn't block
    every future run forever.
    """
    now = datetime.now(timezone.utc)
    stmt = (
        insert(AgentRunLock)
        .values(id=lock_id, locked_at=now, locked_by=run_by)
        .on_conflict_do_update(
            index_elements=[AgentRunLock.id],
            set_={"locked_at": now, "locked_by": run_by},
            where=(AgentRunLock.locked_at.is_(None)) | (AgentRunLock.locked_at < (now - stale_after)),
        )
        .returning(AgentRunLock.id)
    )
    result = session.execute(stmt)
    return result.first() is not None


def release_run_lock(session: Session, run_by: str, lock_id: str = "hourly_batch_run") -> bool:
    """Only the run that holds the lock can release it — a stale/dead
    run's release call (if it somehow woke back up) can't clobber a
    newer run that has since taken the lock over via the staleness
    fallback.
    """
    result = session.execute(
        update(AgentRunLock)
        .where(AgentRunLock.id == lock_id, AgentRunLock.locked_by == run_by)
        .values(locked_at=None, locked_by=None)
    )
    return result.rowcount == 1


def try_acquire_sheet_cell_lock(
    session: Session, held_by: str, cell_key: str, stale_after: timedelta
) -> bool:
    """Decision 034 — serializes concurrent LeadPilot-originated writes
    to the same spreadsheet cell (see models/run_lock.py's module
    docstring for the full threat this closes). Same shape as
    acquire_run_lock: returns True only if the lock was free or stuck
    past `stale_after` by a crashed/hung commit_field_write call.
    `held_by` is a free-text label (rep_id string is expected, not
    enforced) purely for debugging a stuck lock — same convention as
    acquire_run_lock's `run_by`.

    Callers should hold this for the shortest possible window: acquire
    immediately before commit_field_write's read-check-write sequence,
    release in a `finally` right after, whether it succeeded, raised
    StaleWriteError, or hit any other error.

    Unlike acquire_run_lock's typical usage (acquire, commit
    immediately, do the real work in a later, separate transaction),
    commit_field_write acquires and does its work in one still-open
    transaction. That means a second, genuinely concurrent caller's
    own INSERT ... ON CONFLICT here will actually *block* (Postgres
    waits for the first transaction to commit/rollback before
    resolving the conflict), not fail fast — this function can take as
    long as the current holder's full read-check-write sequence, not
    just a fast local check. See connectors/base.py's
    ConcurrentWriteError/StaleWriteError docstrings for which of the
    two errors a blocked-then-unblocked caller actually ends up
    raising once it proceeds.

    Known limitation: the staleness fallback only rescues a lock left
    behind by a transaction that already ended (committed or rolled
    back) in an inconsistent state — it can't help against a
    genuinely still-open, hung transaction (e.g. a caller stuck
    forever on a hanging Sheets API call without the connection
    dropping). That would need a server-side statement/idle-transaction
    timeout, which isn't configured here.
    """
    now = datetime.now(timezone.utc)
    stmt = (
        insert(SheetCellLock)
        .values(cell_key=cell_key, locked_at=now, locked_by=held_by)
        .on_conflict_do_update(
            index_elements=[SheetCellLock.cell_key],
            set_={"locked_at": now, "locked_by": held_by},
            where=(SheetCellLock.locked_at.is_(None)) | (SheetCellLock.locked_at < (now - stale_after)),
        )
        .returning(SheetCellLock.cell_key)
    )
    result = session.execute(stmt)
    return result.first() is not None


def release_sheet_cell_lock(session: Session, held_by: str, cell_key: str) -> bool:
    """Only the holder that acquired the lock can release it — same
    stale-holder-can't-clobber-a-newer-holder guarantee as
    release_run_lock.
    """
    result = session.execute(
        update(SheetCellLock)
        .where(SheetCellLock.cell_key == cell_key, SheetCellLock.locked_by == held_by)
        .values(locked_at=None, locked_by=None)
    )
    return result.rowcount == 1
