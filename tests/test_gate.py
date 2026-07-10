"""Real tests against the real local Postgres — no mocking the
database, since the entire point of the approval gate is that its
correctness depends on actual transaction/row-locking behavior.
"""

import threading
import uuid

from leadpilot import auth, gate
from leadpilot.db import SessionLocal
from leadpilot.models.contact_history import Channel, ContactHistory, Stage, Tool
from leadpilot.models.leads import Lead
from leadpilot.models.rep import Rep


def _make_lead(session) -> uuid.UUID:
    lead = Lead(display_name="Test Lead")
    session.add(lead)
    session.flush()
    return lead.lead_id


def _make_rep(session, email: str = "rep@example.com") -> uuid.UUID:
    rep = auth.create_rep(session, email=f"{uuid.uuid4()}-{email}", password="testpassword123")
    return rep.rep_id


def test_full_lifecycle(db_session):
    lead_id = _make_lead(db_session)
    rep_id = _make_rep(db_session)
    event = gate.create_draft(
        db_session,
        lead_id=lead_id,
        channel=Channel.TEXT,
        tool=Tool.SEND_LEAD_TEXT,
        content_ref="Hi, checking in about your bank statements.",
    )
    assert event.stage == Stage.AWAITING_REP_APPROVAL

    assert gate.approve(db_session, event.event_id, rep_id=rep_id) is True
    db_session.refresh(event)
    assert event.stage == Stage.APPROVED
    assert event.rep_id == rep_id

    assert gate.try_execute(db_session, event.event_id) is True
    db_session.refresh(event)
    assert event.stage == Stage.EXECUTED


def test_cannot_execute_without_approval(db_session):
    lead_id = _make_lead(db_session)
    event = gate.create_draft(
        db_session, lead_id=lead_id, channel=Channel.EMAIL, tool=Tool.SEND_LEAD_EMAIL
    )
    # Never approved — still AWAITING_REP_APPROVAL.
    assert gate.try_execute(db_session, event.event_id) is False
    db_session.refresh(event)
    assert event.stage == Stage.AWAITING_REP_APPROVAL


def test_cannot_execute_twice(db_session):
    lead_id = _make_lead(db_session)
    rep_id = _make_rep(db_session)
    event = gate.create_draft(
        db_session, lead_id=lead_id, channel=Channel.TEXT, tool=Tool.SEND_LEAD_TEXT
    )
    gate.approve(db_session, event.event_id, rep_id=rep_id)

    assert gate.try_execute(db_session, event.event_id) is True
    # Second attempt on the same event — must not fire again.
    assert gate.try_execute(db_session, event.event_id) is False


def test_cannot_approve_an_already_approved_event(db_session):
    lead_id = _make_lead(db_session)
    rep_id_1 = _make_rep(db_session, "rep1@example.com")
    rep_id_2 = _make_rep(db_session, "rep2@example.com")
    event = gate.create_draft(
        db_session, lead_id=lead_id, channel=Channel.TEXT, tool=Tool.SEND_LEAD_TEXT
    )
    assert gate.approve(db_session, event.event_id, rep_id=rep_id_1) is True
    # A second rep (or a double-click) trying to approve the same event again.
    assert gate.approve(db_session, event.event_id, rep_id=rep_id_2) is False


def test_reject_then_cannot_approve_or_execute(db_session):
    lead_id = _make_lead(db_session)
    rep_id = _make_rep(db_session)
    event = gate.create_draft(
        db_session, lead_id=lead_id, channel=Channel.EMAIL, tool=Tool.SEND_LEAD_EMAIL
    )
    assert gate.reject(db_session, event.event_id, rep_id=rep_id) is True
    db_session.refresh(event)
    assert event.stage == Stage.REJECTED

    assert gate.approve(db_session, event.event_id, rep_id=rep_id) is False
    assert gate.try_execute(db_session, event.event_id) is False


def test_lead_id_survives_dedup_style_lookup(db_session):
    """Eval Case 2 requires lead_id to be the canonical, post-dedup
    identifier. This just confirms contact_history actually enforces
    the FK to leads — an orphan lead_id should be rejected by Postgres,
    not silently accepted.
    """
    import pytest
    from sqlalchemy.exc import IntegrityError

    fake_lead_id = uuid.uuid4()
    event = ContactHistory(
        lead_id=fake_lead_id,
        channel=Channel.TEXT,
        tool=Tool.SEND_LEAD_TEXT,
        stage=Stage.AWAITING_REP_APPROVAL,
    )
    db_session.add(event)
    with pytest.raises(IntegrityError):
        db_session.flush()
    # A failed flush leaves the session's transaction unusable — roll
    # back to a clean state so the fixture's own teardown doesn't
    # warn about an already-deassociated transaction.
    db_session.rollback()


def test_try_execute_is_single_use_under_concurrency():
    """The concurrency test flagged as still open in
    leadpilot-docs/decisions/README.md (Decision 021) and
    leadpilot-docs/testing/known-issues-log.md (Issue 003). Fires 10
    real, separate DB connections at the same approved row
    simultaneously — exactly one must win.

    Uses real commits (not the rollback-wrapped db_session fixture)
    since separate connections need to actually see the row.
    """
    setup = SessionLocal()
    lead_id = _make_lead(setup)
    rep_id = _make_rep(setup)
    event = gate.create_draft(
        setup, lead_id=lead_id, channel=Channel.TEXT, tool=Tool.SEND_LEAD_TEXT
    )
    gate.approve(setup, event.event_id, rep_id=rep_id)
    setup.commit()
    event_id = event.event_id
    setup.close()

    results: list[bool] = []
    results_lock = threading.Lock()

    def attempt():
        session = SessionLocal()
        try:
            won = gate.try_execute(session, event_id)
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
            f"expected exactly one winner among {len(threads)} concurrent "
            f"attempts, got {results.count(True)}"
        )
        assert results.count(False) == len(threads) - 1
    finally:
        cleanup = SessionLocal()
        cleanup.query(ContactHistory).filter_by(event_id=event_id).delete()
        cleanup.query(Lead).filter_by(lead_id=lead_id).delete()
        cleanup.query(Rep).filter_by(rep_id=rep_id).delete()
        cleanup.commit()
        cleanup.close()
