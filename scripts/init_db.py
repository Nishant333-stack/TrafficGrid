"""Apply the Render-compatible schema to the configured Postgres database."""

from __future__ import annotations

import os
import sys
import time
import json

from sqlalchemy import create_engine, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.engine import Engine

from backend.config.env_loader import load_project_env
from backend.config.paths import PROJECT_ROOT


def normalize_database_url(url: str) -> str:
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql://", 1)
    if url.startswith("postgresql://") and "+psycopg2" not in url:
        url = url.replace("postgresql://", "postgresql+psycopg2://", 1)
    return url


def apply_schema(database_url: str, retries: int = 5, delay_seconds: float = 3.0) -> Engine:
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
            return engine
        except SQLAlchemyError as exc:
            last_error = exc
            print(
                f"Database init attempt {attempt}/{retries} failed: {exc}",
                flush=True,
            )
            time.sleep(delay_seconds)

    raise SystemExit(f"Unable to initialize database schema: {last_error}") from last_error


def seed_planned_events(engine: Engine) -> None:
    seed_path = PROJECT_ROOT / "planned_events_seed.json"
    if not seed_path.exists():
        return
    events = json.loads(seed_path.read_text(encoding="utf-8"))
    
    with engine.begin() as connection:
        for ev in events:
            # Check if event exists
            exists = connection.execute(
                text("SELECT 1 FROM events WHERE id = :id"),
                {"id": ev["id"]}
            ).scalar()
            
            if not exists:
                connection.execute(
                    text(
                        """
                        INSERT INTO events (id, event_type, latitude, longitude, address, event_cause, start_datetime, status, veh_type, corridor, priority, police_station, zone)
                        VALUES (:id, 'planned', :latitude, :longitude, :name, :event_cause, :scheduled_start, 'upcoming', :veh_type, :corridor, :priority, :police_station, :zone)
                        """
                    ),
                    {
                        "id": ev["id"],
                        "latitude": ev.get("latitude"),
                        "longitude": ev.get("longitude"),
                        "name": ev.get("name"),
                        "event_cause": ev.get("event_cause"),
                        "scheduled_start": ev.get("scheduled_start"),
                        "veh_type": ev.get("veh_type"),
                        "corridor": ev.get("corridor"),
                        "priority": ev.get("priority"),
                        "police_station": ev.get("police_station"),
                        "zone": ev.get("zone"),
                    }
                )
    print("Planned events seeded.", flush=True)


def main() -> None:
    load_project_env()
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        print("WARNING: DATABASE_URL is not set. Skipping database initialization.", flush=True)
        print("The application will fail at runtime if no database is available.", flush=True)
        return
    engine = apply_schema(database_url)
    seed_planned_events(engine)


if __name__ == "__main__":
    main()
    sys.exit(0)
