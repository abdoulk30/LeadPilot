"""Fake-Sheets-service tests for GoogleSheetsConnector.commit_field_write's
concurrent-write protections (Decision 034) — real Postgres for the
sheet_cell_locks table, but no live Google API/network access needed,
using the same injectable-client pattern as the Slack/Gmail/Twilio
tools (sheets_service= constructor param added alongside this fix).

test_google_sheets_connector_live.py still covers the validation-only
and real-live-API paths; this file is specifically about the new
lock/stale-check behavior, which the live test file can't exercise
deterministically (can't easily force two real concurrent writes to
race, or force a live cell to go stale on demand).
"""

import threading
import uuid

import pytest

from leadpilot import auth, google_credentials, locks
from leadpilot.connectors.base import ConcurrentWriteError, StaleWriteError
from leadpilot.connectors.google_drive import SPREADSHEET_MIME_TYPE
from leadpilot.connectors.google_sheets import GoogleSheetsConnector
from leadpilot.db import SessionLocal

from fakes import FakeGoogleDriveClient


class _FakeResponse:
    def __init__(self, data):
        self._data = data

    def execute(self):
        return self._data


class FakeSheetsService:
    """Minimal stand-in for the real `build("sheets", "v4", ...)`
    client. Holds one sheet's data as header + rows in memory;
    `update()` actually mutates it, so tests can assert on real
    before/after state the same way the live tests do against a real
    sheet.
    """

    def __init__(self, header: list[str], rows: dict[str, list[str]]):
        self.header = header
        self.rows = rows  # row_ref -> list of cell values, same order as header
        self.update_calls: list[dict] = []

    def spreadsheets(self):
        return self

    def values(self):
        return self

    def get(self, *, spreadsheetId, range):
        all_rows = [self.header] + [self.rows[ref] for ref in sorted(self.rows, key=int)]
        return _FakeResponse({"values": all_rows})

    def update(self, *, spreadsheetId, range, valueInputOption, body):
        self.update_calls.append({"range": range, "value": body["values"][0][0]})
        # range looks like "C2" — column letter(s) + row number.
        col_letters = "".join(ch for ch in range if ch.isalpha())
        row_ref = "".join(ch for ch in range if ch.isdigit())
        col_index = 0
        for ch in col_letters:
            col_index = col_index * 26 + (ord(ch) - 64)
        col_index -= 1
        self.rows[row_ref][col_index] = body["values"][0][0]
        return _FakeResponse({})


def _make_connected_rep(session, source_id: str) -> uuid.UUID:
    rep = auth.create_rep(session, email=f"{uuid.uuid4()}-rep@example.com", password="testpassword123")
    google_credentials.store_credential(session, rep.rep_id, "fake-refresh-token")
    google_credentials.add_granted_file(session, rep.rep_id, source_id)
    return rep.rep_id


def _connector(session, source_id: str, header=None, rows=None):
    header = header or ["Name", "Phone", "Email", "Company", "Status"]
    rows = rows or {"2": ["Jane Lead", "555-1234", "jane@acme.com", "Acme", "New"]}
    rep_id = _make_connected_rep(session, source_id)
    fake_service = FakeSheetsService(header, rows)
    fake_drive = FakeGoogleDriveClient({source_id: SPREADSHEET_MIME_TYPE})
    connector = GoogleSheetsConnector(session, rep_id, sheets_service=fake_service, drive_client=fake_drive)
    return connector, fake_service


def test_commit_field_write_succeeds_when_expected_current_matches(db_session):
    source_id = f"sheet-{uuid.uuid4()}"
    connector, fake_service = _connector(db_session, source_id)

    connector.commit_field_write(
        source_id, row_ref="2", field_name="status", value="Contacted", expected_current="New"
    )

    assert fake_service.rows["2"][4] == "Contacted"
    assert fake_service.update_calls == [{"range": "E2", "value": "Contacted"}]


def test_commit_field_write_raises_stale_write_error_on_mismatch(db_session):
    """The rep approved a draft built from a diff that's since gone
    stale — the live cell no longer holds what they reviewed.
    """
    source_id = f"sheet-{uuid.uuid4()}"
    connector, fake_service = _connector(db_session, source_id)

    with pytest.raises(StaleWriteError) as exc_info:
        connector.commit_field_write(
            source_id, row_ref="2", field_name="status", value="Contacted", expected_current="Old Value"
        )

    assert exc_info.value.expected == "Old Value"
    assert exc_info.value.actual == "New"
    # Must not have written anything.
    assert fake_service.update_calls == []
    assert fake_service.rows["2"][4] == "New"


def test_commit_field_write_accepts_none_for_a_blank_cell(db_session):
    source_id = f"sheet-{uuid.uuid4()}"
    connector, fake_service = _connector(
        db_session, source_id, rows={"2": ["Jane Lead", "555-1234", "jane@acme.com", "Acme", ""]}
    )

    connector.commit_field_write(
        source_id, row_ref="2", field_name="status", value="Contacted", expected_current=None
    )

    assert fake_service.rows["2"][4] == "Contacted"


def test_commit_field_write_releases_lock_after_success(db_session):
    """Proves the lock doesn't leak — a second write to the same cell
    right after must succeed too, not hang or raise ConcurrentWriteError.
    """
    source_id = f"sheet-{uuid.uuid4()}"
    connector, fake_service = _connector(db_session, source_id)

    connector.commit_field_write(
        source_id, row_ref="2", field_name="status", value="Contacted", expected_current="New"
    )
    connector.commit_field_write(
        source_id, row_ref="2", field_name="status", value="Qualified", expected_current="Contacted"
    )

    assert fake_service.rows["2"][4] == "Qualified"


def test_commit_field_write_releases_lock_after_stale_write_error(db_session):
    """The lock must not leak on the error path either — a StaleWriteError
    shouldn't permanently block future writes to the same cell.
    """
    source_id = f"sheet-{uuid.uuid4()}"
    connector, fake_service = _connector(db_session, source_id)

    with pytest.raises(StaleWriteError):
        connector.commit_field_write(
            source_id, row_ref="2", field_name="status", value="Contacted", expected_current="Wrong"
        )

    # Now a correct write to the same cell must succeed, not raise
    # ConcurrentWriteError from a leaked lock.
    connector.commit_field_write(
        source_id, row_ref="2", field_name="status", value="Contacted", expected_current="New"
    )
    assert fake_service.rows["2"][4] == "Contacted"


def test_commit_field_write_raises_concurrent_write_error_when_lock_held_same_session(db_session):
    """Covers the fail-fast path specifically: a lock already held in
    a *committed-visible-to-this-same-transaction* state. Using the
    same db_session for both the pre-acquire and the connector's call
    (rather than two real separate connections) is deliberate — it's
    what actually reproduces the non-blocking branch deterministically
    in a single-threaded test. See
    test_two_concurrent_commits_to_the_same_cell_only_one_wins for what
    genuinely concurrent, separate-connection callers experience
    instead (StaleWriteError, not this).
    """
    source_id = f"sheet-{uuid.uuid4()}"
    connector, fake_service = _connector(db_session, source_id)

    from datetime import timedelta

    cell_key = f"{source_id}:2:status"
    held = locks.try_acquire_sheet_cell_lock(db_session, "some-other-in-flight-request", cell_key, timedelta(seconds=30))
    assert held is True

    with pytest.raises(ConcurrentWriteError):
        connector.commit_field_write(
            source_id, row_ref="2", field_name="status", value="Contacted", expected_current="New"
        )
    # Must not have written anything — never even got to the read/check.
    assert fake_service.update_calls == []


def test_two_concurrent_commits_to_the_same_cell_only_one_wins():
    """Real end-to-end concurrency test, same rigor as
    test_locks.py's lock tests and test_gate.py's single-use test —
    fires 10 real simultaneous commit_field_write calls at the exact
    same cell against real separate DB connections, all built from the
    same stale-eligible expected_current ("New"). Exactly one must
    actually write; the other nine must fail loudly, never silently
    clobber the winner or corrupt the cell into a mixed/partial value.

    Note on which error the losers get: Postgres's `INSERT ... ON
    CONFLICT` blocks a second, still-uncommitted concurrent
    acquisition rather than failing it fast (see
    leadpilot.locks.try_acquire_sheet_cell_lock's docstring) — so the
    nine losers here don't get rejected at the lock; they queue up,
    each eventually gets its turn, re-reads the cell, and finds
    whichever value the winner already wrote sitting where "New" used
    to be. That's StaleWriteError, not ConcurrentWriteError — this
    test accepts either, since exactly which one fires is a genuine
    implementation detail of Postgres's lock-wait scheduling, but in
    practice expect it to always be StaleWriteError for this exact
    scenario.
    """
    setup = SessionLocal()
    source_id = f"sheet-{uuid.uuid4()}"
    rep_id = _make_connected_rep(setup, source_id)
    setup.commit()
    setup.close()

    fake_service = FakeSheetsService(
        ["Name", "Phone", "Email", "Company", "Status"],
        {"2": ["Jane Lead", "555-1234", "jane@acme.com", "Acme", "New"]},
    )
    # All threads share one fake-service instance on purpose — it's
    # the Postgres-side lock being tested, not per-connector state.
    # Each thread uses its own DB session, same as every other
    # concurrency test in this repo.
    results: list[str] = []
    errors: list[Exception] = []
    results_lock = threading.Lock()

    fake_drive = FakeGoogleDriveClient({source_id: SPREADSHEET_MIME_TYPE})

    def attempt(proposed_value: str):
        session = SessionLocal()
        try:
            connector = GoogleSheetsConnector(session, rep_id, sheets_service=fake_service, drive_client=fake_drive)
            try:
                connector.commit_field_write(
                    source_id, row_ref="2", field_name="status", value=proposed_value, expected_current="New"
                )
                session.commit()
                with results_lock:
                    results.append("won")
            except (StaleWriteError, ConcurrentWriteError):
                session.commit()  # persist the lock release from the finally block above
                with results_lock:
                    results.append("lost")
        except Exception as exc:  # pragma: no cover - safety net so a bug shows up as a failed assertion, not a silent thread death
            with results_lock:
                errors.append(exc)
        finally:
            session.close()

    threads = [threading.Thread(target=attempt, args=(f"value-{i}",)) for i in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == [], f"unexpected exception(s) in worker threads: {errors!r}"
    assert results.count("won") == 1, f"expected exactly one winner among {len(threads)}, got {results.count('won')}"
    assert results.count("lost") == 9
    # The cell must hold exactly the winner's value, never a mix or a
    # value that no thread ever proposed — proves no lost update.
    assert fake_service.rows["2"][4] in {f"value-{i}" for i in range(10)}

    cleanup = SessionLocal()
    from leadpilot.models.rep import Rep
    from leadpilot.models.rep_google_credential import RepGoogleCredential
    from leadpilot.models.run_lock import SheetCellLock

    cleanup.query(SheetCellLock).filter_by(cell_key=f"{source_id}:2:status").delete()
    # This test uses real committed SessionLocal() sessions (not the
    # rollback-wrapped db_session fixture) for genuine Postgres-level
    # concurrency, so the rep it creates via _make_connected_rep
    # doesn't get auto-cleaned by fixture teardown like everywhere
    # else — left behind, it falsely satisfies
    # test_google_sheets_connector_live.py's "is there a real
    # connected rep?" check for every test file that runs after this
    # one, since its stored "fake-refresh-token" credential isn't a
    # real Google refresh token.
    cleanup.query(RepGoogleCredential).filter_by(rep_id=rep_id).delete()
    cleanup.query(Rep).filter_by(rep_id=rep_id).delete()
    cleanup.commit()
    cleanup.close()
