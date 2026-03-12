"""
Database connection and session management.
"""
import logging
from contextlib import contextmanager
from typing import Generator

from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from .config import Config
from .models import Base

logger = logging.getLogger(__name__)


def create_db_engine(config: Config):
    """Create a SQLAlchemy engine.

    SQLite (used in CI/testing) does not support pool_size/max_overflow,
    so we detect it and use NullPool instead.
    """
    url = config.database_url
    if url.startswith("sqlite"):
        from sqlalchemy.pool import StaticPool
        return create_engine(
            url,
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
            echo=False,
        )
    return create_engine(
        url,
        pool_size=5,
        max_overflow=10,
        pool_pre_ping=True,
        echo=False,
    )


def init_db(config: Config) -> sessionmaker:
    """
    Create all tables (if they don't exist) and return a session factory.

    Usage:
        Session = init_db(config)
        with Session() as session:
            ...
    """
    engine = create_db_engine(config)

    # Verify connectivity (skip verbose host/port log for SQLite)
    with engine.connect() as conn:
        conn.execute(text("SELECT 1"))
    url = config.database_url
    if url.startswith("sqlite"):
        logger.info("Database connection OK — %s", url)
    else:
        logger.info("Database connection OK — %s:%s/%s", config.db_host, config.db_port, config.db_name)

    Base.metadata.create_all(engine)
    logger.info("Tables verified / created.")

    return sessionmaker(bind=engine, expire_on_commit=False)


@contextmanager
def get_session(session_factory: sessionmaker) -> Generator[Session, None, None]:
    """Context manager that commits on success and rolls back on error."""
    session: Session = session_factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
