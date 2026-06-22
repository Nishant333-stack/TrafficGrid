import os
from sqlalchemy import create_engine
from sqlalchemy.engine import Engine

_engine: Engine | None = None

def get_engine() -> Engine:
    global _engine
    if _engine is not None:
        return _engine

    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        raise ValueError("DATABASE_URL environment variable is required but not set.")

    if database_url.startswith("postgres://"):
        database_url = database_url.replace("postgres://", "postgresql://", 1)
    if database_url.startswith("postgresql://") and "+psycopg2" not in database_url:
        database_url = database_url.replace("postgresql://", "postgresql+psycopg2://", 1)

    try:
        _engine = create_engine(
            database_url,
            pool_size=10,
            max_overflow=20,
            pool_timeout=30,
            pool_recycle=1800,
            # Validate connections before use so a recycled/idled Postgres
            # connection (common on managed/free tiers) doesn't surface as a
            # 500 ("server closed the connection unexpectedly").
            pool_pre_ping=True,
        )
        return _engine
    except Exception as exc:
        raise RuntimeError(f"Failed to create database engine: {exc}") from exc
