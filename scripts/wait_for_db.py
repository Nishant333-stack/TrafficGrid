"""Block until the configured Postgres accepts connections (or timeout).

Render's free Postgres is frequently not ready the instant the web service
starts deploying. Without this gate, ``init_db.py`` can exhaust its short retry
window, get swallowed by ``start.sh``, and the app boots against missing tables
- serving 500s on every data endpoint while ``/platform/health`` still passes.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

from sqlalchemy import create_engine, text

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.config.env_loader import load_project_env


def normalize_database_url(url: str) -> str:
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql://", 1)
    if url.startswith("postgresql://") and "+psycopg2" not in url:
        url = url.replace("postgresql://", "postgresql+psycopg2://", 1)
    return url


def main() -> int:
    load_project_env()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--timeout", type=float, default=120.0, help="Seconds to wait before giving up.")
    parser.add_argument("--interval", type=float, default=3.0, help="Seconds between attempts.")
    args = parser.parse_args()

    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        print("DATABASE_URL not set; nothing to wait for.", flush=True)
        return 0

    engine = create_engine(normalize_database_url(database_url), pool_pre_ping=True, future=True)
    deadline = time.monotonic() + args.timeout
    attempt = 0
    while True:
        attempt += 1
        try:
            with engine.connect() as connection:
                connection.execute(text("SELECT 1"))
            print(f"Database ready after {attempt} attempt(s).", flush=True)
            return 0
        except Exception as exc:  # noqa: BLE001 - any connection error means "not ready yet"
            if time.monotonic() >= deadline:
                print(f"Database not ready after {args.timeout:.0f}s: {exc}", flush=True)
                return 1
            print(f"  attempt {attempt}: not ready ({exc.__class__.__name__}); retrying...", flush=True)
            time.sleep(args.interval)


if __name__ == "__main__":
    sys.exit(main())
