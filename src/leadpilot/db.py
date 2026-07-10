"""SQLAlchemy engine/session setup.

One engine per process. The Web Service and the Cron Job are separate
containers (Decision 022) but both point at the same DATABASE_URL —
that's what makes the approval-gate conditional update (Decision 021)
actually work across them.
"""

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from leadpilot.config import settings

engine = create_engine(settings.database_url, future=True)

SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)


class Base(DeclarativeBase):
    pass


def get_session() -> Session:
    return SessionLocal()
