from __future__ import annotations

import argparse
import json
import os
import random
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, text

from backend.config.env_loader import load_project_env
from backend.optimization.generate_plan import generate_deployment_plan
from backend.api.main import (
    ensure_feedback_schema,
    planned_event_features,
)
from backend.ml.predict import predict_impact


load_project_env()

from backend.config.paths import PROJECT_ROOT


APP_ROOT = PROJECT_ROOT
PLANNED_EVENTS_PATH = PROJECT_ROOT / "planned_events_seed.json"
SEED_SOURCE = "seed_feedback"
DEFAULT_ROWS = 40


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Seed synthetic feedback rows for first-launch demo metrics."
    )
    parser.add_argument("--rows", type=int, default=DEFAULT_ROWS)
    parser.add_argument(
        "--skip-if-present",
        action="store_true",
        help="No-op if the feedback table already has rows (keeps cold starts fast)",
    )
    return parser.parse_args()


def feedback_count() -> int:
    """Best-effort count of existing feedback rows (0 if the table/DB is unavailable)."""
    try:
        from sqlalchemy import text

        from backend.config.db import get_engine

        with get_engine().connect() as connection:
            return int(connection.execute(text("SELECT count(*) FROM feedback")).scalar() or 0)
    except Exception:
        return 0


def jsonable(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: jsonable(item) for key, item in value.items()}
    if isinstance(value, list):
        return [jsonable(item) for item in value]
    if isinstance(value, datetime):
        return value.isoformat()
    if hasattr(value, "item"):
        try:
            return value.item()
        except (AttributeError, ValueError):
            return value
    return value


def load_planned_seed_events() -> list[dict[str, Any]]:
    return json.loads(PLANNED_EVENTS_PATH.read_text(encoding="utf-8"))


def demo_plan() -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    seed_event = planned_event_features(load_planned_seed_events()[0])
    forecast = predict_impact(seed_event)
    event_with_forecast = dict(seed_event)
    event_with_forecast.update(forecast)

    try:
        plan = generate_deployment_plan(event_with_forecast)
    except Exception:
        plan = {
            "control_points": [
                {
                    "node_id": 25,
                    "lat": seed_event["latitude"],
                    "lon": seed_event["longitude"],
                    "lane_estimate": 4,
                    "is_arterial": True,
                    "personnel_needed": 3,
                    "barricades_needed": 0,
                    "reasoning": ["base staffing: 2 officers", "lane_estimate >= 3: +1 officer"],
                }
            ],
            "total_personnel": 3,
            "total_barricades": 0,
            "allocations": [
                {
                    "control_point_node_id": 25,
                    "station_id": 12,
                    "station_name": "Cubbon Park",
                    "personnel_assigned": 3,
                    "distance_m": 730.0,
                }
            ],
            "shortfall": 0,
            "diversions": [],
        }
    return seed_event, forecast, plan


def synthetic_feedback_rows(
    event_ids: list[str],
    row_count: int,
    seed_event: dict[str, Any],
    forecast: dict[str, Any],
    plan: dict[str, Any],
) -> list[dict[str, Any]]:
    rng = random.Random(20260618)
    now = datetime.now(UTC)
    rows: list[dict[str, Any]] = []
    base_predicted = max(25, int(round(float(forecast["duration_median"]))))

    for index in range(row_count):
        predicted = max(10, int(base_predicted * rng.uniform(0.45, 1.55)))
        actual = max(8, int(predicted * rng.uniform(0.72, 1.28) + rng.randint(-12, 18)))
        accepted = index == 0 or rng.random() < 0.68
        row_plan = plan if index == 0 else None
        adjusted_personnel = None
        if accepted and rng.random() < 0.18:
            adjusted_personnel = max(2, int(plan.get("total_personnel", 12)) + rng.choice([-2, -1, 1, 2]))

        rows.append(
            {
                "event_id": event_ids[index % len(event_ids)],
                "event_name": seed_event.get("name", "City marathon, MG Road") if index == 0 else f"Synthetic feedback {index:02d}",
                "predicted_severity": "HIGH" if rng.random() < 0.28 else "LOW",
                "predicted_duration_minutes": predicted,
                "actual_duration_minutes": actual,
                "officer_rating": rng.choices([3, 4, 5, 2], weights=[30, 42, 24, 4], k=1)[0],
                "plan_accepted": accepted,
                "adjusted_personnel": adjusted_personnel,
                "plan_total_personnel": int(adjusted_personnel or plan.get("total_personnel") or 0),
                "plan_json": row_plan,
                "seed_source": SEED_SOURCE,
                "created_at": (now - timedelta(days=rng.randint(0, 27), hours=rng.randint(0, 23))).isoformat(),
            }
        )

    rows[0]["event_id"] = event_ids[0]
    rows[0]["event_name"] = seed_event.get("name", "City marathon, MG Road")
    rows[0]["predicted_duration_minutes"] = int(round(float(forecast["duration_median"])))
    rows[0]["actual_duration_minutes"] = max(10, int(rows[0]["predicted_duration_minutes"] * 0.92))
    rows[0]["officer_rating"] = 5
    rows[0]["plan_accepted"] = True
    rows[0]["adjusted_personnel"] = None
    rows[0]["plan_total_personnel"] = int(plan.get("total_personnel") or 0)
    rows[0]["plan_json"] = plan
    rows[0]["created_at"] = now.isoformat()
    return rows


def seed_local(rows: list[dict[str, Any]]) -> None:
    pass


def db_event_ids(database_url: str, fallback_count: int) -> list[str]:
    engine = create_engine(database_url, future=True)
    query = text(
        """
        SELECT id
        FROM events
        ORDER BY start_datetime DESC NULLS LAST
        LIMIT :limit
        """
    )
    with engine.connect() as connection:
        ids = [str(row[0]) for row in connection.execute(query, {"limit": fallback_count}).all()]
    if not ids:
        raise SystemExit("No events found in Postgres. Run load_data.py before seed_feedback.py.")
    return ids


def seed_database(database_url: str, rows: list[dict[str, Any]]) -> None:
    engine = create_engine(database_url, future=True)
    ensure_feedback_schema(engine)
    with engine.begin() as connection:
        connection.execute(text("DELETE FROM feedback WHERE seed_source = :seed_source"), {"seed_source": SEED_SOURCE})
        for row in rows:
            connection.execute(
                text(
                    """
                    INSERT INTO feedback (
                        event_id,
                        event_name,
                        predicted_severity,
                        predicted_duration_minutes,
                        actual_duration_minutes,
                        officer_rating,
                        plan_accepted,
                        adjusted_personnel,
                        plan_total_personnel,
                        plan_json,
                        seed_source,
                        created_at
                    )
                    VALUES (
                        :event_id,
                        :event_name,
                        :predicted_severity,
                        :predicted_duration_minutes,
                        :actual_duration_minutes,
                        :officer_rating,
                        :plan_accepted,
                        :adjusted_personnel,
                        :plan_total_personnel,
                        CAST(:plan_json AS JSONB),
                        :seed_source,
                        :created_at
                    )
                    """
                ),
                {
                    **row,
                    "plan_json": json.dumps(jsonable(row.get("plan_json"))),
                    "created_at": row["created_at"],
                },
            )
    print(f"Seeded {len(rows)} synthetic feedback rows into Postgres feedback.")


def main() -> None:
    args = parse_args()
    # Skip the whole (model-loading) seed path on warm reboots if data exists.
    if args.skip_if_present:
        existing = feedback_count()
        if existing >= 20:
            print(f"feedback already populated ({existing} rows); skipping seed.", flush=True)
            return
    seed_event, forecast, plan = demo_plan()
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        raise ValueError("DATABASE_URL environment variable is required.")
        
    event_ids = db_event_ids(database_url, args.rows)

    rows = synthetic_feedback_rows(event_ids, args.rows, seed_event, forecast, plan)

    seed_database(database_url, rows)


if __name__ == "__main__":
    main()
