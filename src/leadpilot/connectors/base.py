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
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from sqlalchemy.orm import Session


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
    def commit_field_write(self, source_id: str, row_ref: str, field_name: str, value: str) -> None:
        """Perform the real write. Caller is responsible for having
        confirmed rep approval first (leadpilot.gate.try_execute).
        """

    @abstractmethod
    def detect_changes(self, source_id: str, session: Session) -> ChangesSummary:
        """Compare the source's current rows against
        leadpilot.models.dedup.LeadSourceRow (what was seen on the
        last run) and return what's new or updated.
        """
