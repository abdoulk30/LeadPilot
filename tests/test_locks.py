"""Real tests against the real local Postgres for all three locks in
leadpilot.locks.
"""

import threading
import uuid
from datetime import timedelta

from leadpilot import auth, locks
from leadpilot.db import SessionLocal
from leadpilot.models.leads import Lead
from leadpilot.models.rep import Rep, RepSession
from leadpilot.models.run_lock import AgentRunLock, LeadActionLock, SheetCellLock


def _make_lead(session) -> uuid.UUID:
    lead = Lead(display_name="Lock Test Lead")
    session.add(lead)
    session.flush()
    return lead.lead_id


def _make_rep(session) -> uuid.UUID:
    rep = auth.create_rep(session, email=f"{uuid.uuid4()}-lock-test@example.com", password="testpassword123")
    return rep.rep_id


def test_lead_action_lock_first_acquire_succeeds(db_session):
    lead_id = _make_lead(db_session)
    assert locks.try_acquire_lead_action_lock(db_session, lead_id, cooldown=timedelta(minutes=15)) is True


def test_lead_action_lock_blocks_within_cooldown(db_session):
    lead_id = _make_lead(db_session)
    assert locks.try_acquire_lead_action_lock(db_session, lead_id, cooldown=timedelta(minutes=15)) is True
    # Same lead, immediately again, same cooldown — must be blocked.
    assert locks.try_acquire_lead_action_lock(db_session, lead_id, cooldown=timedelta(minutes=15)) is False


def test_lead_action_lock_allows_after_cooldown_elapsed(db_session):
    lead_id = _make_lead(db_session)
    assert locks.try_acquire_lead_action_lock(db_session, lead_id, cooldown=timedelta(minutes=15)) is True
    # A cooldown of zero means "anything already committed counts as
    # expired" — proves the WHERE clause actually re-evaluates against
    # a real elapsed-time comparison, not just "row exists = blocked".
    assert locks.try_acquire_lead_action_lock(db_session, lead_id, cooldown=timedelta(seconds=0)) is True


def test_lead_action_lock_is_single_use_under_concurrency():
    """The exact scenario security/pen-test-checklist.md names: 'Two
    run cycles triggered in rapid succession against the same lead —
    confirm the atomic lock actually prevents a double-send.' Fires 10
    real concurrent attempts to acquire the same lead's action lock;
    exactly one must win.
    """
    setup = SessionLocal()
    lead_id = _make_lead(setup)
    setup.commit()
    setup.close()

    results: list[bool] = []
    results_lock = threading.Lock()

    def attempt():
        session = SessionLocal()
        try:
            won = locks.try_acquire_lead_action_lock(session, lead_id, cooldown=timedelta(minutes=15))
            session.commit()
            with results_lock:
                results.append(won)
        finally:
            session.close()

    threads = [threading.Thread(target=attempt) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    try:
        assert results.count(True) == 1, (
            f"expected exactly one of {len(threads)} concurrent lead-action-lock "
            f"acquisitions to win, got {results.count(True)}"
        )
    finally:
        cleanup = SessionLocal()
        cleanup.query(LeadActionLock).filter_by(lead_id=lead_id).delete()
        cleanup.query(Lead).filter_by(lead_id=lead_id).delete()
        cleanup.commit()
        cleanup.close()


def test_run_lock_acquire_and_release(db_session):
    rep_id = _make_rep(db_session)
    assert locks.acquire_run_lock(db_session, rep_id, run_by="run-a", stale_after=timedelta(hours=2)) is True
    # Still held — a second run for the same rep must not acquire it.
    assert locks.acquire_run_lock(db_session, rep_id, run_by="run-b", stale_after=timedelta(hours=2)) is False
    assert locks.release_run_lock(db_session, rep_id, run_by="run-a") is True
    # Now free again.
    assert locks.acquire_run_lock(db_session, rep_id, run_by="run-b", stale_after=timedelta(hours=2)) is True


def test_run_lock_release_only_by_holder(db_session):
    rep_id = _make_rep(db_session)
    locks.acquire_run_lock(db_session, rep_id, run_by="run-a", stale_after=timedelta(hours=2))
    # run-b never held it — must not be able to release run-a's lock.
    assert locks.release_run_lock(db_session, rep_id, run_by="run-b") is False
    assert locks.acquire_run_lock(db_session, rep_id, run_by="run-c", stale_after=timedelta(hours=2)) is False


def test_run_lock_stale_lock_is_reclaimed(db_session):
    rep_id = _make_rep(db_session)
    # A run that acquired the lock and then crashed without releasing.
    locks.acquire_run_lock(db_session, rep_id, run_by="crashed-run", stale_after=timedelta(seconds=0))
    # stale_after=0 means "immediately eligible for reclaim" — proves
    # a dead run can't block the cron job forever.
    assert locks.acquire_run_lock(db_session, rep_id, run_by="new-run", stale_after=timedelta(seconds=0)) is True


def test_run_lock_does_not_block_a_different_rep(db_session):
    """The actual point of the per-rep rework (Decision 027/032): rep
    A's run being in progress must never block rep B's run — the old
    singleton design would have blocked this.
    """
    rep_a = _make_rep(db_session)
    rep_b = _make_rep(db_session)
    assert locks.acquire_run_lock(db_session, rep_a, run_by="run-a", stale_after=timedelta(hours=2)) is True
    # Rep A's lock is held — rep B's must be completely unaffected.
    assert locks.acquire_run_lock(db_session, rep_b, run_by="run-b", stale_after=timedelta(hours=2)) is True


def test_run_lock_is_single_use_under_concurrency():
    """The batch-run equivalent of the lead-action concurrency test —
    two overlapping Cron Job invocations for the *same* rep must not
    both win the lock.
    """
    setup = SessionLocal()
    rep_id = _make_rep(setup)
    setup.commit()
    setup.close()

    results: list[bool] = []
    results_lock = threading.Lock()

    def attempt(run_id: str):
        session = SessionLocal()
        try:
            won = locks.acquire_run_lock(session, rep_id, run_by=run_id, stale_after=timedelta(hours=2))
            session.commit()
            with results_lock:
                results.append(won)
        finally:
            session.close()

    threads = [threading.Thread(target=attempt, args=(f"run-{i}",)) for i in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    try:
        assert results.count(True) == 1, (
            f"expected exactly one of {len(threads)} concurrent run-lock acquisitions "
            f"to win, got {results.count(True)}"
        )
    finally:
        cleanup = SessionLocal()
        cleanup.query(AgentRunLock).filter_by(rep_id=rep_id).delete()
        cleanup.query(RepSession).filter_by(rep_id=rep_id).delete()
        cleanup.query(Rep).filter_by(rep_id=rep_id).delete()
        cleanup.commit()
        cleanup.close()


def test_sheet_cell_lock_acquire_and_release(db_session):
    cell_key = f"test_sheet:{uuid.uuid4()}:2:status"
    assert locks.try_acquire_sheet_cell_lock(db_session, "rep-a", cell_key, stale_after=timedelta(seconds=30)) is True
    # Still held — a second commit to the exact same cell must not win.
    assert locks.try_acquire_sheet_cell_lock(db_session, "rep-b", cell_key, stale_after=timedelta(seconds=30)) is False
    assert locks.release_sheet_cell_lock(db_session, "rep-a", cell_key) is True
    # Now free again.
    assert locks.try_acquire_sheet_cell_lock(db_session, "rep-b", cell_key, stale_after=timedelta(seconds=30)) is True


def test_sheet_cell_lock_release_only_by_holder(db_session):
    cell_key = f"test_sheet:{uuid.uuid4()}:2:status"
    locks.try_acquire_sheet_cell_lock(db_session, "rep-a", cell_key, stale_after=timedelta(seconds=30))
    # rep-b never held it — must not be able to release rep-a's lock.
    assert locks.release_sheet_cell_lock(db_session, "rep-b", cell_key) is False
    assert locks.try_acquire_sheet_cell_lock(db_session, "rep-c", cell_key, stale_after=timedelta(seconds=30)) is False


def test_sheet_cell_lock_stale_lock_is_reclaimed(db_session):
    cell_key = f"test_sheet:{uuid.uuid4()}:2:status"
    # A commit_field_write call that acquired the lock and then crashed
    # (or the Sheets API call hung) without releasing.
    locks.try_acquire_sheet_cell_lock(db_session, "crashed-commit", cell_key, stale_after=timedelta(seconds=0))
    # stale_after=0 means "immediately eligible for reclaim" — proves a
    # stuck lock can't permanently block writes to that cell.
    assert locks.try_acquire_sheet_cell_lock(db_session, "new-commit", cell_key, stale_after=timedelta(seconds=0)) is True


def test_sheet_cell_lock_is_single_use_under_concurrency():
    """The spreadsheet-write equivalent of the other two concurrency
    tests above — two reps' concurrent commit_field_write calls to the
    exact same cell must not both proceed. This is the mechanism
    behind Decision 034's fix for the "two reps edit the same lead's
    row at once" race found while discussing concurrent spreadsheet
    access.
    """
    cell_key = f"test_sheet:{uuid.uuid4()}:2:status"
    results: list[bool] = []
    results_lock = threading.Lock()

    def attempt(rep_id: str):
        session = SessionLocal()
        try:
            won = locks.try_acquire_sheet_cell_lock(session, rep_id, cell_key, stale_after=timedelta(hours=2))
            session.commit()
            with results_lock:
                results.append(won)
        finally:
            session.close()

    threads = [threading.Thread(target=attempt, args=(f"rep-{i}",)) for i in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    try:
        assert results.count(True) == 1, (
            f"expected exactly one of {len(threads)} concurrent sheet-cell-lock "
            f"acquisitions to win, got {results.count(True)}"
        )
    finally:
        cleanup = SessionLocal()
        cleanup.query(SheetCellLock).filter_by(cell_key=cell_key).delete()
        cleanup.commit()
        cleanup.close()
