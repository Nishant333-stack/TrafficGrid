"""Render-safe ingestion of the bundled Astram CSV into the events table.

Unlike ``load_astram.py`` (which applies the PostGIS-backed ``schema.sql`` and
runs a ``geom``-based sanity check), this loader targets Render's managed
Postgres, which has **no PostGIS extension**. It therefore:

  * applies ``schema_render.sql`` (plain columns, no ``geom``),
  * upserts events and reseeds police stations from the CSV,
  * skips the PostGIS ``print_sanity()`` check.

It is idempotent and cheap to re-run: if the events table is already populated
above ``--min-rows`` it exits immediately, so container cold-starts stay fast.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from sqlalchemy import create_engine, text

# Allow running as `python scripts/load_events.py` without PYTHONPATH set.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.config.env_loader import load_project_env
from backend.config.paths import PROJECT_ROOT
from backend.data.load_data import (
    apply_schema,
    load_csv,
    reseed_police_stations,
    upsert_events,
)


def normalize_database_url(url: str) -> str:
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql://", 1)
    if url.startswith("postgresql://") and "+psycopg2" not in url:
        url = url.replace("postgresql://", "postgresql+psycopg2://", 1)
    return url


def find_astram_csv() -> Path | None:
    """Locate the bundled Astram CSV in the project root."""
    for pattern in ("*Astram*event*data*.csv", "*astram*event*data*.csv", "*.csv"):
        for csv_file in sorted(PROJECT_ROOT.glob(pattern)):
            name = csv_file.name.lower()
            if "astram" in name and "event" in name:
                return csv_file
    return None


def existing_event_count(engine) -> int:
    try:
        with engine.connect() as connection:
            return int(connection.execute(text("SELECT COUNT(*) FROM events")).scalar_one())
    except Exception:
        # Table may not exist yet; schema is applied below.
        return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--csv",
        type=Path,
        default=None,
        help="Path to the Astram CSV (auto-detected in the project root if omitted).",
    )
    parser.add_argument(
        "--min-rows",
        type=int,
        default=100,
        help="Skip loading if the events table already has at least this many rows.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Load even if the events table is already populated.",
    )
    return parser.parse_args()


def main() -> int:
    load_project_env()
    args = parse_args()

    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        print("WARNING: DATABASE_URL is not set; skipping CSV ingestion.", flush=True)
        return 0

    engine = create_engine(normalize_database_url(database_url), future=True)

    # Ensure the render-safe schema exists (idempotent; safe even if init_db ran).
    apply_schema(engine, PROJECT_ROOT / "schema_render.sql")

    current = existing_event_count(engine)
    if current >= args.min_rows and not args.force:
        print(
            f"events already populated ({current} rows >= {args.min_rows}); skipping CSV load.",
            flush=True,
        )
        return 0

    csv_path = args.csv or find_astram_csv()
    if not csv_path or not csv_path.exists():
        print("WARNING: Astram CSV not found; skipping CSV ingestion.", flush=True)
        return 0

    print(f"Loading events from {csv_path.name} ...", flush=True)
    data, missing_id, duplicate_id, invalid_duration = load_csv(csv_path)
    if missing_id:
        print(f"  dropped {missing_id} rows with missing id", flush=True)
    if duplicate_id:
        print(f"  kept last of {duplicate_id} duplicate ids", flush=True)
    if invalid_duration:
        print(f"  nulled {invalid_duration} invalid durations (negative or >24h)", flush=True)

    upsert_events(engine, data)
    station_count = reseed_police_stations(engine, data)

    with engine.connect() as connection:
        total = int(connection.execute(text("SELECT COUNT(*) FROM events")).scalar_one())
        planned = int(
            connection.execute(
                text("SELECT COUNT(*) FROM events WHERE event_type = 'planned'")
            ).scalar_one()
        )
    print(
        f"Loaded {len(data)} CSV rows -> {total} events ({planned} planned), "
        f"{station_count} police stations.",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
