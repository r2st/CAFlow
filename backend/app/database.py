"""SQLAlchemy engine, session factory and declarative base."""

from __future__ import annotations

import logging
from collections.abc import Iterator

from sqlalchemy import create_engine, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import settings

logger = logging.getLogger(__name__)


def _engine_kwargs(url: str) -> dict:
    # SQLite (used by the test-suite) needs a couple of dialect-specific knobs
    # and has no meaningful pool to size.
    if url.startswith("sqlite"):
        return {"connect_args": {"check_same_thread": False}}
    return {
        # Cheap liveness check before a connection is handed out: without it, a
        # connection killed server-side surfaces as a failed request.
        "pool_pre_ping": True,
        "pool_size": settings.db_pool_size,
        "max_overflow": settings.db_max_overflow,
        "pool_recycle": settings.db_pool_recycle_seconds,
        "pool_timeout": settings.db_pool_timeout_seconds,
        # Names the connection in pg_stat_activity, which is what you want
        # when the API and the Celery worker share a database.
        "connect_args": {"application_name": settings.app_name},
    }


engine = create_engine(
    settings.database_url,
    future=True,
    echo=settings.db_echo,
    **_engine_kwargs(settings.database_url),
)
SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


def get_db() -> Iterator[Session]:
    """FastAPI dependency yielding a request-scoped session.

    Rolls back on the way out of a failed request: a session returned to the
    pool mid-transaction poisons the next request that borrows it.
    """
    db = SessionLocal()
    try:
        yield db
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def check_database() -> tuple[bool, str | None]:
    """Round-trip a trivial query. Used by the readiness probe."""
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
        return True, None
    except SQLAlchemyError as exc:
        logger.warning("Database health check failed: %s", exc)
        return False, type(exc).__name__


def pool_status() -> dict[str, int | str]:
    """Pool gauges for the readiness payload, when the dialect has a pool."""
    pool = engine.pool
    if not hasattr(pool, "size"):
        return {"kind": type(pool).__name__}
    return {
        "kind": type(pool).__name__,
        "size": pool.size(),
        "checked_in": pool.checkedin(),
        "checked_out": pool.checkedout(),
        "overflow": pool.overflow(),
    }
