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
    """Create a SQLAlchemy engine with sensible pool settings."""
    return create_engine(
        config.database_url,
        pool_size=5,
        max_overflow=10,
        pool_pre_ping=True,  # verify connections before using them
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

    # Verify we can connect before proceeding
    with engine.connect() as conn:
        conn.execute(text("SELECT 1"))
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
