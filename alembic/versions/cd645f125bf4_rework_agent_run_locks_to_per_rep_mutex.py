"""rework agent_run_locks to per-rep mutex

Revision ID: cd645f125bf4
Revises: d2caf87d6b35
Create Date: 2026-07-12 14:39:14.329880

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'cd645f125bf4'
down_revision: Union[str, Sequence[str], None] = 'd2caf87d6b35'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Autogenerate missed the primary-key move entirely (added rep_id
    # as a plain NOT NULL column, dropped id, never touched the PK
    # constraint — would have left the table with no primary key at
    # all). No real data exists yet in this dev-only, pre-launch table,
    # so drop-and-recreate is simpler and safer than a careful in-place
    # ALTER for a schema this early.
    op.drop_table('agent_run_locks')
    op.create_table(
        'agent_run_locks',
        sa.Column('rep_id', sa.UUID(), nullable=False),
        sa.Column('locked_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('locked_by', sa.String(), nullable=True),
        sa.ForeignKeyConstraint(['rep_id'], ['reps.rep_id']),
        sa.PrimaryKeyConstraint('rep_id'),
    )


def downgrade() -> None:
    op.drop_table('agent_run_locks')
    op.create_table(
        'agent_run_locks',
        sa.Column('id', sa.String(), nullable=False),
        sa.Column('locked_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('locked_by', sa.String(), nullable=True),
        sa.PrimaryKeyConstraint('id'),
    )
