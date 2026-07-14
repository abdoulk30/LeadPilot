"""LeadSourceConnector interface — PRD v1.04 section 3e / Decision 015.

Keeps prioritization/next-best-action logic from hard-coding
Sheets-specific assumptions, so a later source (Excel, OnlyOffice)
doesn't require reworking the tools that consume this interface. Only
GoogleSheetsConnector exists — see google_sheets.py.

Interpretation note on write_field: the PRD describes it as staging a
diff, "commits only after rep approval." That approval gating is
Decision 021's mechanism (leadpilot.gate / leadpilot.locks), which
operates on contact_history rows, not on the connector itself. So this
interface splits the PRD's single "write_field" into two explicit
steps instead of one method with implicit gating baked in:

  - stage_field_write(): computes and returns the diff. Never writes.
  - commit_field_write(): performs the real write. Step 2's
    update_lead_sheet tool is expected to call this only after
    gate.try_execute() has returned True for the corresponding
    contact_history row — the connector itself has no opinion about
    approval state, it just trusts the caller to have checked.

This keeps the same separation of concerns already established for
every other side-effect tool (drafts happen freely, real effects are
gated at the contact_history/gate.py layer, not scattered per-tool).

Concurrent-write note (Decision 034): a rep can approve a draft built
from a diff that's since gone stale — another rep approved a
conflicting edit to the same cell first, or someone edited the sheet
directly in Google's UI, bypassing LeadPilot entirely. commit_field_write
takes the `expected_current` value the rep actually saw and reviewed
(what stage_field_write returned as FieldDiff.current at draft time)
and MUST verify it still matches the cell's live value immediately
before writing, raising StaleWriteError if not — never silently
overwrite. Implementations are also expected to serialize concurrent
commits to the very same cell (GoogleSheetsConnector does this via
leadpilot.locks.try_acquire_sheet_cell_lock), raising
ConcurrentWriteError if a commit to that exact cell is already
in-flight rather than racing it. Callers (Step 3's approval endpoint)
must treat both as "show the rep a fresh diff and ask them to
re-approve," not as a transient error worth silently retrying with the
same stale value.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from sqlalchemy.orm import Session


class ConcurrentWriteError(RuntimeError):
    """Raised by commit_field_write when it can tell *without waiting*
    that another write already holds the same cell's lock in a
    committed-but-not-yet-stale state. In practice this is the rarer
    of the two failure modes: real cross-request concurrency on the
    same cell almost always surfaces as StaleWriteError instead (see
    that class) — Postgres's `INSERT ... ON CONFLICT` blocks a second,
    still-uncommitted concurrent transaction rather than failing it
    fast, so the common case is "wait for the other write to finish,
    then notice the value changed," not "reject immediately." This
    error is the fail-fast path for the narrower case of a lock left
    in a locked-but-committed state (e.g. a caller that reuses
    leadpilot.locks.try_acquire_sheet_cell_lock/release_sheet_cell_lock
    directly across separate transactions, the way AgentRunLock is
    used, rather than in one atomic unit the way commit_field_write
    does). The caller should tell the rep to retry shortly either way.
    """

    def __init__(self, source_id: str, row_ref: str, field: str):
        self.source_id = source_id
        self.row_ref = row_ref
        self.field = field
        super().__init__(
            f"Another write to {source_id!r} row {row_ref!r} field {field!r} "
            "is already in progress — try again shortly."
        )


class StaleWriteError(RuntimeError):
    """Raised by commit_field_write when the cell's live value no
    longer matches `expected_current` — the value the rep actually
    reviewed and approved has changed since. Never write over this
    silently; the caller must re-show the rep a fresh diff.

    This is the failure mode that actually catches two reps racing to
    edit the same cell: Postgres blocks the second commit_field_write
    call until the first one's transaction commits, then the second
    call's freshness check runs against the now-updated value and
    fails here — see ConcurrentWriteError's docstring for why that one
    rarely fires for this specific scenario. It also catches edits
    made directly in Google's UI, bypassing LeadPilot (and its lock)
    entirely — nothing in this system can lock a cell against manual
    edits in Google Sheets itself, so the value check is the only
    defense for that case.
    """

    def __init__(self, source_id: str, row_ref: str, field: str, expected: str | None, actual: str | None):
        self.source_id = source_id
        self.row_ref = row_ref
        self.field = field
        self.expected = expected
        self.actual = actual
        super().__init__(
            f"{source_id!r} row {row_ref!r} field {field!r} changed since the rep approved this "
            f"edit — expected {expected!r}, found {actual!r}."
        )


@dataclass
class LeadRecord:
    source_id: str
    row_ref: str
    name: str | None
    phone: str | None
    email: str | None
    company: str | None
    status: str | None
    raw: dict[str, str] = field(default_factory=dict)


@dataclass
class FieldDiff:
    source_id: str
    row_ref: str
    field: str
    current: str | None
    proposed: str


@dataclass
class ChangesSummary:
    new_rows: list[LeadRecord]
    updated_rows: list[LeadRecord]


class LeadSourceConnector(ABC):
    @abstractmethod
    def list_sources(self) -> list[str]:
        """Enumerate the configured lead sources."""

    @abstractmethod
    def fetch_rows(self, source_id: str) -> list[LeadRecord]:
        """Return structured lead records for one source."""

    @abstractmethod
    def stage_field_write(self, source_id: str, row_ref: str, field_name: str, value: str) -> FieldDiff:
        """Compute a current-vs-proposed diff for a single field.
        Never writes — see the module docstring for why this is split
        from commit_field_write.
        """

    @abstractmethod
    def commit_field_write(
        self, source_id: str, row_ref: str, field_name: str, value: str, *, expected_current: str | None
    ) -> None:
        """Perform the real write. Caller is responsible for having
        confirmed rep approval first (leadpilot.gate.try_execute).

        `expected_current` is required (not defaulted) deliberately —
        see the module docstring's "Concurrent-write note" (Decision
        034). Pass the exact FieldDiff.current value the rep reviewed,
        including None if the cell was blank at draft time. Raises
        StaleWriteError if the live value has since changed, or
        ConcurrentWriteError if another commit to the same cell is
        already in flight.
        """

    @abstractmethod
    def detect_changes(self, source_id: str, session: Session) -> ChangesSummary:
        """Compare the source's current rows against
        leadpilot.models.dedup.LeadSourceRow (what was seen on the
        last run) and return what's new or updated.
        """
