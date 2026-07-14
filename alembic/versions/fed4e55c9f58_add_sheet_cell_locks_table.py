"""add sheet_cell_locks table

Revision ID: fed4e55c9f58
Revises: d2caf87d6b35
Create Date: 2026-07-13 18:20:00.000000

Decision 034 — closes the same-cell concurrent-write race condition in
GoogleSheetsConnector.commit_field_write. Same shape as agent_run_locks
(models/run_lock.py), keyed by a "{source_id}:{row_ref}:{field_name}"
string instead of a fixed lock name.

**DO NOT RUN AGAINST main WITHOUT CHECKING THIS FIRST:** this was
written against d2caf87d6b35 as head because that's the newest
migration present in this local checkout. leadpilot-docs/decisions/
README.md's "Migration-head correction" note claims a *different*,
newer migration already exists and is merged upstream —
cd645f125bf4_rework_agent_run_locks_to_per_rep_mutex.py (Decision
027's per-rep AgentRunLock rework) — but that file isn't reachable
anywhere in this checkout's git history, branches, or remote-tracking
refs (confirmed via `git rev-list --objects --all`, `git show-ref`).
Before running `alembic upgrade head`: pull the real latest main/
abdouls-branch from GitHub directly and run `alembic heads` — if
cd645f125bf4 (or anything newer) turns out to be the real head, edit
this file's down_revision below to point at it instead of
d2caf87d6b35, or Alembic will see two divergent heads and refuse to
upgrade until a manual `alembic merge` reconciles them.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'fed4e55c9f58'
down_revision: Union[str, Sequence[str], None] = 'd2caf87d6b35'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table('sheet_cell_locks',
    sa.Column('cell_key', sa.String(), nullable=False),
    sa.Column('locked_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('locked_by', sa.String(), nullable=True),
    sa.PrimaryKeyConstraint('cell_key')
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table('sheet_cell_locks')
