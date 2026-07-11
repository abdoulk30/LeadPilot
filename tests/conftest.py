"""Shared test fixtures.

Runs against the real local dev Postgres (scripts/devdb.sh), not a
mock — the whole point of the approval gate is that its correctness
depends on real database transaction/locking semantics, which a mock
can't verify. Each test gets a session wrapped in a transaction that's
rolled back afterward, so tests don't leave data behind or interfere
with each other.
"""

import pytest
from sqlalchemy.orm import Session

from leadpilot.db import engine


@pytest.fixture()
def db_session():
    connection = engine.connect()
    transaction = connection.begin()
    # join_transaction_mode="create_savepoint" means a rollback inside
    # the test (e.g. after an expected IntegrityError) only undoes a
    # nested SAVEPOINT, not the outer transaction this fixture uses
    # for isolation — without it, an in-test rollback tears down the
    # same transaction object this fixture rolls back again in
    # teardown, which SQLAlchemy warns about.
    session = Session(bind=connection, join_transaction_mode="create_savepoint")
    try:
        yield session
    finally:
        session.close()
        transaction.rollback()
        connection.close()
