"""add message_type column to contact_history

Revision ID: d006ca87ab77
Revises: fed4e55c9f58
Create Date: 2026-07-13 21:54:37.953785

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'd006ca87ab77'
down_revision: Union[str, Sequence[str], None] = 'fed4e55c9f58'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Alembic's autogenerate emits a bare sa.Enum(...) inline on
# add_column, but unlike create_table (which implicitly issues CREATE
# TYPE as part of the table DDL), add_column alone does NOT create the
# backing Postgres enum type first — confirmed by actually running this
# migration, not assumed: it failed with "type message_type does not
# exist" on a real local Postgres. Declaring it as its own object and
# calling .create()/.drop() explicitly is the correct fix.
message_type_enum = sa.Enum(
    'completion_handoff', 'info_request', 'urgent_callback_request', name='message_type'
)


def upgrade() -> None:
    """Upgrade schema."""
    message_type_enum.create(op.get_bind())
    op.add_column('contact_history', sa.Column('message_type', message_type_enum, nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('contact_history', 'message_type')
    message_type_enum.drop(op.get_bind())
