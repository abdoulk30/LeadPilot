"""add sheet_cell_locks table

Revision ID: fed4e55c9f58
Revises: cd645f125bf4
Create Date: 2026-07-13 18:20:00.000000

Decision 034 — closes the same-cell concurrent-write race condition in
GoogleSheetsConnector.commit_field_write. Same shape as agent_run_locks
(models/run_lock.py), keyed by a "{source_id}:{row_ref}:{field_name}"
string instead of a fixed lock name.

Originally written against d2caf87d6b35 as down_revision, before
cd645f125bf4_rework_agent_run_locks_to_per_rep_mutex.py (Decision 027's
per-rep AgentRunLock rework) was reachable from this checkout. Now that
abdouls-branch has been merged in and cd645f125bf4 confirmed as the
real intervening head, down_revision below points at it directly —
this stays a single linear chain rather than needing a separate
`alembic merge` revision.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'fed4e55c9f58'
down_revision: Union[str, Sequence[str], None] = 'cd645f125bf4'
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
