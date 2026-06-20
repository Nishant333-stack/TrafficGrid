"""Apply the Render-compatible schema to the configured Postgres database."""

from __future__ import annotations

import os
import sys
import time

from sqlalchemy import create_engine, text
from sqlalchemy.exc import SQLAlchemyError

from backend.config.env_loader import load_project_env
from backend.config.paths import PROJECT_ROOT


def normalize_database_url(url: str) -> str:
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql://", 1)
    if url.startswith("postgresql://") and "+psycopg2" not in url:
        url = url.replace("postgresql://", "postgresql+psycopg2://", 1)
    return url


def apply_schema(database_url: str, retries: int = 5, delay_seconds: float = 3.0) -> None:
    schema_path = PROJECT_ROOT / "schema_render.sql"
    statements = [
        statement.strip()
        for statement in schema_path.read_text(encoding="utf-8").split(";")
        if statement.strip()
    ]
    engine = create_engine(normalize_database_url(database_url), future=True)

    last_error: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            with engine.begin() as connection:
                for statement in statements:
                    connection.execute(text(statement))
            print("Database schema applied successfully.", flush=True)
            return
        except SQLAlchemyError as exc:
            last_error = exc
            print(
                f"Database init attempt {attempt}/{retries} failed: {exc}",
                flush=True,
            )
            time.sleep(delay_seconds)

    raise SystemExit(f"Unable to initialize database schema: {last_error}") from last_error


def main() -> None:
    load_project_env()
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        print("DATABASE_URL is not set; skipping database initialization.", flush=True)
        return
    apply_schema(database_url)


if __name__ == "__main__":
    main()
    sys.exit(0)
