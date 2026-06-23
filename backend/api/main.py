from __future__ import annotations

import asyncio
import json
import os
import threading
import time
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd
from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError

from backend.config.env_loader import load_project_env
from backend.config.db import get_engine
from backend.config.paths import FRONTEND_DIST, PROJECT_ROOT


load_project_env()

from backend.optimization.generate_plan import generate_deployment_plan
from backend.integrations.integrations import (
    all_feed_records,
    integration_status,
    live_incidents,
    operational_context_for_event,
    planned_permits,
)
from backend.ml.model_monitoring import drift_summary, forecast_backtest_summary, retrain_plan
from backend.optimization.multi_incident import build_multi_incident_plan
from backend.monitoring.operational_monitoring import operational_metrics_snapshot
from backend.monitoring.platform_ops import (
    platform_health,
    retention_dry_run,
    retention_policy,
    security_controls,
)
from backend.ml.predict import predict_impact
from backend.monitoring.roi_metrics import executive_roi_summary
from backend.data.workflow import (
    after_action_csv,
    after_action_report,
    all_audit_logs,
    all_field_status_rows,
    audit_log,
    create_plan_record,
    plan_history,
    record_field_status,
    sla_summary,
    update_plan_approval,
)


APP_ROOT = PROJECT_ROOT
PLANNED_EVENTS_PATH = PROJECT_ROOT / "planned_events_seed.json"
HISTORICAL_CACHE_PATH = PROJECT_ROOT / "backend" / "ml" / "models" / "training_events_preprocessed.parquet"

EVENT_COLUMNS = """
    id,
    event_type,
    latitude,
    longitude,
    address,
    event_cause,
    requires_road_closure,
    start_datetime,
    status,
    description,
    veh_type,
    corridor,
    priority,
    police_station,
    zone,
    junction,
    duration_minutes
"""


class FeedbackRequest(BaseModel):
    accepted: bool
    adjusted_personnel: int | None = Field(default=None, ge=0)
    officer_rating: int | None = Field(default=None, ge=1, le=5)
    actual_duration_minutes: int | None = Field(default=None, ge=0)
    plan: dict[str, Any] | None = None


class MultiIncidentPlanRequest(BaseModel):
    event_ids: list[str] | None = None
    scenarios: list[str] | None = None


class PlanWorkflowRequest(BaseModel):
    event_id: str
    plan: dict[str, Any]
    actor: str = "command.operator"
    tenant_id: str = "bengaluru-traffic"


class ApprovalRequest(BaseModel):
    action: str = Field(pattern="^(submit|approve|reject|activate|close)$")
    actor: str = "traffic.commander"
    tenant_id: str = "bengaluru-traffic"
    comment: str | None = None


class FieldStatusRequest(BaseModel):
    station: str
    event_id: str
    control_point_node_id: str | int
    status: str
    actor: str = "field.officer"
    tenant_id: str = "bengaluru-traffic"
    lat: float | None = None
    lon: float | None = None
    note: str | None = None
    photo_url: str | None = None


def cors_allow_origins() -> list[str]:
    origins = [
        "http://localhost",
        "http://localhost:3000",
        "http://localhost:5173",
        "http://127.0.0.1",
        "http://127.0.0.1:3000",
        "http://127.0.0.1:5173",
    ]
    extra = os.environ.get("CORS_ALLOW_ORIGINS", "")
    if extra:
        origins.extend(part.strip() for part in extra.split(",") if part.strip())
    render_url = os.environ.get("RENDER_EXTERNAL_URL")
    if render_url:
        origins.append(render_url.rstrip("/"))
    return origins


app = FastAPI(
    title="Bengaluru Traffic Forecasting MVP API",
    version="0.4.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=cors_allow_origins(),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

_engine_error: str | None = None
STARTED_AT = datetime.now(UTC)
REQUEST_STATS = {
    "requests_total": 0,
    "errors_total": 0,
    "latency_ms_total": 0.0,
}
FORECAST_CACHE_TTL_SECONDS = int(os.environ.get("FORECAST_CACHE_TTL_SECONDS", "300"))
PLAN_CACHE_TTL_SECONDS = int(os.environ.get("PLAN_CACHE_TTL_SECONDS", "600"))
ACTIVE_EVENT_LOOKBACK_DAYS_RAW = os.environ.get("ACTIVE_EVENT_LOOKBACK_DAYS")
ACTIVE_EVENT_LOOKBACK_DAYS = int(ACTIVE_EVENT_LOOKBACK_DAYS_RAW) if ACTIVE_EVENT_LOOKBACK_DAYS_RAW else None
ACTIVE_EVENT_LIMIT = int(os.environ.get("ACTIVE_EVENT_LIMIT", "1000"))
ACTIVE_EVENT_INCLUDE_DEMO_FEEDS = os.environ.get("ACTIVE_EVENT_INCLUDE_DEMO_FEEDS", "").lower() in {
    "1",
    "true",
    "yes",
}
_FORECAST_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
_PLAN_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}


class RequestContext(BaseModel):
    tenant_id: str
    user_id: str
    role: str


def request_context(
    x_tenant_id: str = Header(default="bengaluru-traffic"),
    x_user_id: str = Header(default="local-demo-user"),
    x_user_role: str = Header(default="traffic_commander"),
) -> RequestContext:
    return RequestContext(
        tenant_id=x_tenant_id,
        user_id=x_user_id,
        role=x_user_role,
    )


def require_roles(*allowed_roles: str):
    def dependency(context: RequestContext = Depends(request_context)) -> RequestContext:
        if context.role not in allowed_roles and context.role != "admin":
            raise HTTPException(status_code=403, detail="Insufficient role for this action")
        return context

    return dependency


def normalize_database_url(url: str) -> str:
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql://", 1)
    if url.startswith("postgresql://") and "+psycopg2" not in url:
        url = url.replace("postgresql://", "postgresql+psycopg2://", 1)
    return url


@app.on_event("startup")
def ensure_schema_on_startup() -> None:
    """Guarantee the core tables exist before serving traffic.

    This is a safety net for the deploy path: if ``scripts/init_db.py`` failed
    or was skipped (e.g. the managed Postgres was not ready in time), the app
    would otherwise serve 500s against missing tables while ``/platform/health``
    (a bare ``SELECT 1``) still reports healthy. Applying ``schema_render.sql``
    is idempotent (every statement is ``CREATE TABLE IF NOT EXISTS`` /
    ``INSERT ... ON CONFLICT DO NOTHING``), so re-running it is cheap and never
    destroys data.
    """
    if not os.environ.get("DATABASE_URL"):
        return
    schema_path = PROJECT_ROOT / "schema_render.sql"
    if not schema_path.exists():
        return
    try:
        engine = get_engine()
        with engine.begin() as connection:
            connection.exec_driver_sql(schema_path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001 - never block startup on this safety net
        print(f"WARNING: startup schema ensure failed: {exc}", flush=True)


@app.middleware("http")
async def observe_requests(request: Request, call_next):
    start = time.perf_counter()
    REQUEST_STATS["requests_total"] += 1
    try:
        response = await call_next(request)
    except Exception:
        REQUEST_STATS["errors_total"] += 1
        raise
    finally:
        REQUEST_STATS["latency_ms_total"] += (time.perf_counter() - start) * 1000.0
    if response.status_code >= 500:
        REQUEST_STATS["errors_total"] += 1
    response.headers["X-Grid-Request-Observed"] = "true"
    return response


def jsonable(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: jsonable(item) for key, item in value.items()}
    if isinstance(value, list):
        return [jsonable(item) for item in value]
    if isinstance(value, (datetime, pd.Timestamp)):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if hasattr(value, "item"):
        try:
            return value.item()
        except (AttributeError, ValueError):
            return value
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    return value


def parse_datetime(value: Any) -> datetime | None:
    timestamp = pd.to_datetime(value, errors="coerce", utc=True)
    if pd.isna(timestamp):
        return None
    return timestamp.to_pydatetime()


def load_planned_events() -> list[dict[str, Any]]:
    engine = get_engine()
    from sqlalchemy import text
    with engine.connect() as conn:
        result = conn.execute(text("SELECT * FROM events WHERE event_type = 'planned'"))
        events = []
        for r in result:
            events.append({
                "id": r.id,
                "name": r.address,
                "latitude": r.latitude,
                "longitude": r.longitude,
                "scheduled_start": r.start_datetime.isoformat() if r.start_datetime else None,
                "event_cause": r.event_cause,
                "corridor": r.corridor,
                "zone": r.zone,
                "police_station": r.police_station,
                "veh_type": r.veh_type,
                "priority": r.priority,
            })
        return events


def scheduled_start_value(event: dict[str, Any]) -> Any:
    return event.get("scheduled_start") or event.get("scheduled_start_time")


def is_planned_event(event: dict[str, Any]) -> bool:
    """Classify an event as planned (rally/festival/sports/construction/permit)
    vs unplanned (crash/breakdown/etc.), per the problem statement's two classes."""
    event_type = str(event.get("event_type") or "").strip().lower()
    if event_type:
        return event_type == "planned"
    status = str(event.get("status") or "").strip().lower()
    return status in {"planned", "upcoming", "scheduled"}


def planned_event_response(event: dict[str, Any]) -> dict[str, Any]:
    response = dict(event)
    scheduled_start = scheduled_start_value(event)
    response["scheduled_start"] = scheduled_start
    response["scheduled_start_time"] = scheduled_start
    return response


def planned_event_features(event: dict[str, Any]) -> dict[str, Any]:
    features = planned_event_response(event)
    features["start_datetime"] = scheduled_start_value(event)
    features["status"] = "planned"
    features.setdefault("requires_road_closure", None)
    return features


def row_mapping_to_features(row: dict[str, Any]) -> dict[str, Any]:
    features = {key: jsonable(value) for key, value in row.items()}
    return features


def lookup_db_event(event_id: str) -> dict[str, Any] | None:
    engine = get_engine()
    query = text(f"SELECT {EVENT_COLUMNS} FROM events WHERE id = :event_id")
    with engine.connect() as connection:
        row = connection.execute(query, {"event_id": event_id}).mappings().first()
    return row_mapping_to_features(dict(row)) if row else None


def lookup_seed_event(event_id: str) -> dict[str, Any] | None:
    for event in load_planned_events():
        if event["id"] == event_id:
            return planned_event_features(event)
    return None


def lookup_integrated_event(event_id: str) -> dict[str, Any] | None:
    for event in [*live_incidents(), *planned_permits()]:
        if event.get("id") == event_id:
            if event.get("status") == "planned":
                return planned_event_features(event)
            return dict(event)
    return None


def get_event_or_404(event_id: str) -> dict[str, Any]:
    event = lookup_db_event(event_id) or lookup_integrated_event(event_id) or lookup_seed_event(event_id)
    if event is None:
        raise HTTPException(status_code=404, detail=f"Unknown event_id: {event_id}")
    return event


def event_cache_signature(event_id: str, event: dict[str, Any]) -> str:
    signature_fields = [
        event_id,
        event.get("status"),
        event.get("event_cause"),
        event.get("priority"),
        event.get("corridor"),
        event.get("zone"),
        event.get("police_station"),
        event.get("start_datetime") or event.get("scheduled_start"),
        event.get("requires_road_closure"),
        event.get("latitude"),
        event.get("longitude"),
    ]
    return "|".join(str(jsonable(value)) for value in signature_fields)


def cached_value(cache: dict[str, tuple[float, dict[str, Any]]], key: str, ttl_seconds: int) -> dict[str, Any] | None:
    cached = cache.get(key)
    if not cached:
        return None
    cached_at, value = cached
    if time.monotonic() - cached_at > ttl_seconds:
        cache.pop(key, None)
        return None
    return dict(value)


def remember_value(cache: dict[str, tuple[float, dict[str, Any]]], key: str, value: dict[str, Any]) -> dict[str, Any]:
    cache[key] = (time.monotonic(), dict(value))
    return value


def active_events() -> list[dict[str, Any]]:
    cutoff = datetime.now(UTC) - timedelta(days=ACTIVE_EVENT_LOOKBACK_DAYS) if ACTIVE_EVENT_LOOKBACK_DAYS else None
    date_filter = "AND start_datetime >= :cutoff" if cutoff else ""
    query = text(
        f"""
        SELECT
            'events_database' AS source,
            id,
            latitude,
            longitude,
            event_cause,
            priority,
            corridor,
            zone,
            police_station,
            start_datetime,
            status
        FROM events
        WHERE status = 'active'
            AND latitude IS NOT NULL
            AND longitude IS NOT NULL
            {date_filter}
        ORDER BY start_datetime DESC NULLS LAST
        LIMIT :limit
        """
    )
    params = {"limit": ACTIVE_EVENT_LIMIT}
    if cutoff:
        params["cutoff"] = cutoff
    # Degrade gracefully: if the database is unset/unreachable (e.g. DATABASE_URL
    # not yet wired, or the managed DB is warming up), serve live feeds instead
    # of 500ing.
    db_events: list[dict[str, Any]] = []
    try:
        engine = get_engine()
        with engine.connect() as connection:
            rows = connection.execute(query, params).mappings().all()
        db_events = [jsonable(dict(row)) for row in rows]
    except Exception as exc:  # noqa: BLE001 - degrade rather than fail the request
        print(f"active_events: database unavailable, using live feeds only ({exc})", flush=True)
        if not ACTIVE_EVENT_INCLUDE_DEMO_FEEDS:
            return []

    if not ACTIVE_EVENT_INCLUDE_DEMO_FEEDS:
        return db_events

    seen_ids = {str(event.get("id")) for event in db_events}
    feed_events = [
        jsonable(event)
        for event in live_incidents()
        if str(event.get("id")) not in seen_ids
    ]
    return [*feed_events, *db_events]


def event_hour(value: Any) -> int:
    timestamp = parse_datetime(value)
    return timestamp.hour if timestamp else -1


def normalize_match_value(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip().lower()


def hour_distance(left: int, right: int) -> float:
    if left < 0 or right < 0:
        return 2.0
    delta = abs(left - right)
    return min(delta, 24 - delta) / 6.0


def load_historical_events() -> pd.DataFrame:
    engine = get_engine()
    query = text(
        f"""
        SELECT {EVENT_COLUMNS}
        FROM events
        WHERE event_cause IS NOT NULL
           OR corridor IS NOT NULL
        """
    )
    df = pd.read_sql_query(query, engine)
    if not df.empty:
        return df

    if HISTORICAL_CACHE_PATH.exists():
        return pd.read_parquet(HISTORICAL_CACHE_PATH)

    return pd.DataFrame()


def similar_events(event_features: dict[str, Any], limit: int = 5) -> list[dict[str, Any]]:
    history = load_historical_events()
    if history.empty:
        return []

    if "hour_of_day" not in history.columns:
        history["hour_of_day"] = pd.to_datetime(
            history.get("start_datetime"), errors="coerce", utc=True
        ).dt.hour.fillna(-1).astype(int)

    target_cause = normalize_match_value(event_features.get("event_cause"))
    target_corridor = normalize_match_value(event_features.get("corridor"))
    target_hour = event_hour(event_features.get("start_datetime"))
    target_id = str(event_features.get("id", ""))

    scored: list[dict[str, Any]] = []
    for _, row in history.iterrows():
        row_id = str(row.get("id", ""))
        if row_id and row_id == target_id:
            continue

        cause = normalize_match_value(row.get("event_cause"))
        corridor = normalize_match_value(row.get("corridor"))
        hour = int(row.get("hour_of_day") if not pd.isna(row.get("hour_of_day")) else -1)
        score = 0.0
        score += 3.0 if cause != target_cause else 0.0
        score += 2.0 if corridor != target_corridor else 0.0
        score += hour_distance(target_hour, hour)

        scored.append(
            {
                "id": row_id,
                "event_cause": jsonable(row.get("event_cause")),
                "corridor": jsonable(row.get("corridor")),
                "start_datetime": jsonable(row.get("start_datetime")),
                "duration_minutes": jsonable(row.get("duration_minutes")),
                "requires_road_closure": jsonable(row.get("requires_road_closure")),
                "match_score": round(score, 3),
            }
        )

    scored.sort(key=lambda item: (item["match_score"], str(item["id"])))
    return scored[:limit]


def forecast_event(event_id: str) -> dict[str, Any]:
    event = get_event_or_404(event_id)
    cache_key = event_cache_signature(event_id, event)
    cached = cached_value(_FORECAST_CACHE, cache_key, FORECAST_CACHE_TTL_SECONDS)
    if cached is not None:
        cached["cache_status"] = "hit"
        return cached

    forecast = predict_impact(event)
    planned = is_planned_event(event)
    payload = {
        "event_id": event_id,
        **jsonable(forecast),
        # Make the planned vs unplanned distinction explicit for the dashboard.
        "event_class": "planned" if planned else "unplanned",
        "event_type": event.get("event_type") or ("planned" if planned else "unplanned"),
        "scheduled_start": jsonable(scheduled_start_value(event) or event.get("start_datetime"))
        if planned
        else None,
        "event_name": event.get("name")
        or event.get("event_name")
        or (event.get("address") if planned else None),
        "operational_context": jsonable(operational_context_for_event(event)),
        "similar_events": similar_events(event, limit=5),
        "cache_status": "miss",
    }
    return remember_value(_FORECAST_CACHE, cache_key, payload)


def plan_event(event_id: str) -> dict[str, Any]:
    event = get_event_or_404(event_id)
    cache_key = event_cache_signature(event_id, event)
    cached = cached_value(_PLAN_CACHE, cache_key, PLAN_CACHE_TTL_SECONDS)
    if cached is not None:
        cached["plan_cache_status"] = "hit"
        return cached

    forecast_payload = forecast_event(event_id)
    forecast = {
        key: value
        for key, value in forecast_payload.items()
        if key not in {"event_id", "similar_events", "operational_context", "cache_status"}
    }
    merged = dict(event)
    merged.update(forecast)
    plan = jsonable(generate_deployment_plan(merged))
    plan["plan_cache_status"] = "miss"
    plan["forecast_cache_status"] = forecast_payload.get("cache_status")
    return remember_value(_PLAN_CACHE, cache_key, plan)


def ensure_feedback_schema(engine: Engine) -> None:
    statements = [
        "ALTER TABLE feedback ADD COLUMN IF NOT EXISTS adjusted_personnel INTEGER",
        "ALTER TABLE feedback ADD COLUMN IF NOT EXISTS plan_total_personnel INTEGER",
        "ALTER TABLE feedback ADD COLUMN IF NOT EXISTS plan_json JSONB",
        "ALTER TABLE feedback ADD COLUMN IF NOT EXISTS seed_source TEXT",
        "ALTER TABLE feedback ADD COLUMN IF NOT EXISTS event_name TEXT",
    ]
    with engine.begin() as connection:
        for statement in statements:
            connection.execute(text(statement))


def plan_for_feedback(
    event: dict[str, Any],
    forecast: dict[str, Any],
    supplied_plan: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if supplied_plan is not None:
        return supplied_plan

    merged = dict(event)
    merged.update(forecast)
    try:
        return generate_deployment_plan(merged)
    except Exception:
        return {}
    return int(plan.get("total_personnel") or 0)


def ensure_event_row_for_feedback(connection, event_id: str, event: dict[str, Any]) -> None:
    exists = connection.execute(
        text("SELECT 1 FROM events WHERE id = :event_id"),
        {"event_id": event_id},
    ).first()
    if exists:
        return

    start_datetime = parse_datetime(
        event.get("start_datetime")
        or event.get("scheduled_start")
        or event.get("scheduled_start_time")
    )
    connection.execute(
        text(
            """
            INSERT INTO events (
                id,
                event_type,
                latitude,
                longitude,
                address,
                event_cause,
                requires_road_closure,
                start_datetime,
                status,
                description,
                veh_type,
                corridor,
                priority,
                police_station,
                zone,
                junction
            )
            VALUES (
                :id,
                :event_type,
                :latitude,
                :longitude,
                :address,
                :event_cause,
                :requires_road_closure,
                :start_datetime,
                :status,
                :description,
                :veh_type,
                :corridor,
                :priority,
                :police_station,
                :zone,
                :junction
            )
            ON CONFLICT (id) DO NOTHING
            """
        ),
        {
            "id": event_id,
            "event_type": event.get("event_type") or "planned",
            "latitude": event.get("latitude"),
            "longitude": event.get("longitude"),
            "address": event.get("address") or event.get("name"),
            "event_cause": event.get("event_cause"),
            "requires_road_closure": event.get("requires_road_closure"),
            "start_datetime": start_datetime,
            "status": event.get("status") or "planned",
            "description": event.get("description") or event.get("name"),
            "veh_type": event.get("veh_type"),
            "corridor": event.get("corridor"),
            "priority": event.get("priority"),
            "police_station": event.get("police_station"),
            "zone": event.get("zone"),
            "junction": event.get("junction"),
        },
    )


def write_feedback(event_id: str, payload: FeedbackRequest) -> dict[str, Any]:
    event = get_event_or_404(event_id)
    forecast = predict_impact(event)
    predicted_duration = int(round(float(forecast["duration_median"])))
    plan_json = plan_for_feedback(event, forecast, payload.plan)
    plan_total = (
        payload.adjusted_personnel
        if payload.adjusted_personnel is not None
        else int(plan_json.get("total_personnel") or 0)
    )
    created_at = datetime.now(UTC)

    engine = get_engine()
    ensure_feedback_schema(engine)
    with engine.begin() as connection:
        ensure_event_row_for_feedback(connection, event_id, event)
        connection.execute(
            text(
                """
                INSERT INTO feedback (
                    event_id,
                    predicted_severity,
                    predicted_duration_minutes,
                    actual_duration_minutes,
                    officer_rating,
                    plan_accepted,
                    adjusted_personnel,
                    plan_total_personnel,
                    plan_json,
                    event_name,
                    created_at
                )
                VALUES (
                    :event_id,
                    :predicted_severity,
                    :predicted_duration_minutes,
                    :actual_duration_minutes,
                    :officer_rating,
                    :plan_accepted,
                    :adjusted_personnel,
                    :plan_total_personnel,
                    CAST(:plan_json AS JSONB),
                    :event_name,
                    :created_at
                )
                """
            ),
            {
                "event_id": event_id,
                "predicted_severity": forecast["severity_label"],
                "predicted_duration_minutes": predicted_duration,
                "actual_duration_minutes": payload.actual_duration_minutes,
                "officer_rating": payload.officer_rating,
                "plan_accepted": payload.accepted,
                "adjusted_personnel": payload.adjusted_personnel,
                "plan_total_personnel": plan_total,
                "plan_json": json.dumps(jsonable(plan_json)),
                "event_name": event.get("name") or event.get("event_cause") or event_id,
                "created_at": created_at,
            },
        )

    audit_log(
        "feedback.recorded",
        "officer.mobile",
        "bengaluru-traffic",
        "event",
        event_id,
        {
            "accepted": payload.accepted,
            "adjusted_personnel": payload.adjusted_personnel,
            "officer_rating": payload.officer_rating,
        },
    )
    return {
        "event_id": event_id,
        "accepted": payload.accepted,
        "predicted_duration_minutes": predicted_duration,
        "plan_total_personnel": plan_total,
        "stored": True,
    }


def planned_events_today() -> int:
    today = datetime.now().date()
    count = 0
    for event in load_planned_events():
        scheduled = parse_datetime(scheduled_start_value(event))
        if scheduled and scheduled.date() == today:
            count += 1
    return count


def feedback_accuracy_from_rows(rows: list[dict[str, Any]]) -> float | None:
    cutoff = datetime.now(UTC) - timedelta(days=30)
    errors = []
    for row in rows:
        created = parse_datetime(row.get("created_at"))
        predicted = row.get("predicted_duration_minutes")
        actual = row.get("actual_duration_minutes")
        if created is None or created < cutoff or predicted in (None, 0) or actual in (None, 0):
            continue
        errors.append(abs(float(predicted) - float(actual)) / abs(float(actual)))
    if not errors:
        return None
    return round(float(sum(errors) / len(errors) * 100.0), 2)


def db_metrics_summary(engine: Engine) -> dict[str, Any]:
    ensure_feedback_schema(engine)
    active_ids = {str(event.get("id")) for event in active_events()}
    cutoff = datetime.now(UTC) - timedelta(days=ACTIVE_EVENT_LOOKBACK_DAYS) if ACTIVE_EVENT_LOOKBACK_DAYS else None
    date_filter = "AND start_datetime >= :cutoff" if cutoff else ""
    count_params = {"cutoff": cutoff} if cutoff else {}
    with engine.connect() as connection:
        active_count = connection.execute(
            text(
                f"""
                SELECT COUNT(*)
                FROM events
                WHERE status = 'active'
                    AND latitude IS NOT NULL
                    AND longitude IS NOT NULL
                    {date_filter}
                """
            ),
            count_params,
        ).scalar_one()
        personnel_rows = connection.execute(
            text(
                """
                SELECT
                    event_id,
                    adjusted_personnel,
                    plan_total_personnel
                FROM feedback
                WHERE plan_accepted IS TRUE
                """
            )
        ).mappings().all()
        feedback_rows = connection.execute(
            text(
                """
                SELECT
                    predicted_duration_minutes,
                    actual_duration_minutes,
                    created_at
                FROM feedback
                WHERE created_at >= NOW() - INTERVAL '30 days'
                """
            )
        ).mappings().all()

    personnel = sum(
        int(row.get("adjusted_personnel") or row.get("plan_total_personnel") or 0)
        for row in personnel_rows
        if str(row.get("event_id")) in active_ids
    )
    active_total = int(active_count)
    if ACTIVE_EVENT_INCLUDE_DEMO_FEEDS:
        active_total += len(live_incidents())
    return {
        "active_incident_count": active_total,
        "planned_events_today": planned_events_today(),
        "total_personnel_deployed": int(personnel or 0),
        "forecast_accuracy_30d": feedback_accuracy_from_rows(
            [dict(row) for row in feedback_rows]
        ),
    }


def metrics_summary() -> dict[str, Any]:
    # Degrade gracefully when the database is unset/unreachable so the dashboard
    # shows zeros instead of 500ing.
    try:
        engine = get_engine()
        return db_metrics_summary(engine)
    except Exception as exc:  # noqa: BLE001 - degrade rather than fail the request
        print(f"metrics_summary: database unavailable, returning zeros ({exc})", flush=True)
        return {
            "active_incident_count": len(live_incidents()) if ACTIVE_EVENT_INCLUDE_DEMO_FEEDS else 0,
            "planned_events_today": 0,
            "total_personnel_deployed": 0,
            "forecast_accuracy_30d": None,
            "generated_at": datetime.now(UTC).isoformat(),
            "degraded": True,
        }


def parse_plan_json(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else None
        except json.JSONDecodeError:
            return None
    return None


def station_assignments_from_feedback_rows(
    rows: list[dict[str, Any]],
    station_name: str,
) -> dict[str, Any]:
    station_key = station_name.strip().lower()
    for row in rows:
        plan = parse_plan_json(row.get("plan_json"))
        if not plan:
            continue

        matching_allocations = [
            allocation
            for allocation in plan.get("allocations", [])
            if str(allocation.get("station_name", "")).strip().lower() == station_key
        ]
        if not matching_allocations:
            continue

        control_points_by_id = {
            str(point.get("node_id")): point
            for point in plan.get("control_points", [])
        }
        assignments = []
        for allocation in matching_allocations:
            node_id = str(allocation.get("control_point_node_id"))
            point = control_points_by_id.get(node_id, {})
            assignments.append(
                {
                    "control_point_node_id": allocation.get("control_point_node_id"),
                    "lat": point.get("lat"),
                    "lon": point.get("lon"),
                    "personnel_assigned": allocation.get("personnel_assigned", 0),
                    "distance_m": allocation.get("distance_m"),
                    "lane_estimate": point.get("lane_estimate"),
                    "is_arterial": point.get("is_arterial"),
                    "reasoning": point.get("reasoning", []),
                }
            )

        return {
            "station_name": station_name,
            "event_id": row.get("event_id"),
            "event_name": row.get("event_name") or row.get("event_id"),
            "created_at": jsonable(row.get("created_at")),
            "assignments": assignments,
        }

    return {
        "station_name": station_name,
        "event_id": None,
        "event_name": None,
        "created_at": None,
        "assignments": [],
    }


def field_assignments(station_name: str = "Cubbon Park") -> dict[str, Any]:
    engine = get_engine()
    ensure_feedback_schema(engine)
    with engine.connect() as connection:
        rows = connection.execute(
            text(
                """
                SELECT
                    event_id,
                    plan_json,
                    event_name,
                    created_at
                FROM feedback
                WHERE plan_accepted IS TRUE
                    AND plan_json IS NOT NULL
                ORDER BY created_at DESC
                LIMIT 50
                """
            )
        ).mappings().all()
    return station_assignments_from_feedback_rows([dict(row) for row in rows], station_name)


def feedback_rows_for_roi() -> list[dict[str, Any]] | None:
    engine = get_engine()
    ensure_feedback_schema(engine)
    with engine.connect() as connection:
        rows = connection.execute(
            text(
                """
                SELECT
                    event_id,
                    predicted_severity,
                    predicted_duration_minutes,
                    actual_duration_minutes,
                    officer_rating,
                    plan_accepted,
                    adjusted_personnel,
                    plan_total_personnel,
                    plan_json,
                    event_name,
                    created_at
                FROM feedback
                WHERE created_at >= NOW() - INTERVAL '30 days'
                ORDER BY created_at DESC
                """
            )
        ).mappings().all()
    return [jsonable(dict(row)) for row in rows]


@app.get("/")
def get_api_root():
    from fastapi.responses import RedirectResponse
    return RedirectResponse(url="/app/")


@app.get("/events/active")
def get_active_events() -> list[dict[str, Any]]:
    return active_events()


@app.get("/events/upcoming")
def get_upcoming_events() -> list[dict[str, Any]]:
    events_by_id: dict[str, dict[str, Any]] = {}
    for event in [*load_planned_events(), *planned_permits()]:
        events_by_id[str(event["id"])] = planned_event_response(event)
    return jsonable(list(events_by_id.values()))


@app.get("/integrations/status")
def get_integrations_status() -> list[dict[str, Any]]:
    return integration_status()


@app.get("/integrations/snapshot")
def get_integrations_snapshot() -> dict[str, Any]:
    return jsonable(all_feed_records())


@app.get("/security/context")
def get_security_context(context: RequestContext = Depends(request_context)) -> dict[str, Any]:
    return {
        **context.model_dump(),
        "controls": security_controls()["rbac"].get(context.role, []),
    }


def database_connected() -> bool:
    engine = get_engine()
    if engine is None:
        return False
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
        return True
    except Exception:
        return False


@app.get("/platform/health")
def get_platform_health() -> dict[str, Any]:
    health = platform_health(
        database_connected=database_connected(),
        integration_count=len(integration_status()),
    )
    health["database_error"] = _engine_error
    return health


@app.get("/platform/observability")
def get_platform_observability() -> dict[str, Any]:
    uptime_seconds = max((datetime.now(UTC) - STARTED_AT).total_seconds(), 1.0)
    requests_total = int(REQUEST_STATS["requests_total"])
    return {
        "started_at": STARTED_AT.isoformat(),
        "uptime_seconds": round(uptime_seconds, 1),
        "requests_total": requests_total,
        "errors_total": int(REQUEST_STATS["errors_total"]),
        "average_latency_ms": round(
            REQUEST_STATS["latency_ms_total"] / max(requests_total, 1),
            2,
        ),
        "environment": os.environ.get("APP_ENV", "local"),
    }


@app.get("/platform/retention")
def get_platform_retention() -> dict[str, Any]:
    return retention_policy()


@app.post("/platform/retention/run")
def post_platform_retention_run(
    context: RequestContext = Depends(require_roles("admin")),
) -> dict[str, Any]:
    audit_log("retention.dry_run", context.user_id, context.tenant_id, "platform", "retention", {})
    return retention_dry_run()


@app.get("/platform/security-review")
def get_platform_security_review() -> dict[str, Any]:
    return security_controls()


@app.get("/models/backtest")
def get_model_backtest() -> dict[str, Any]:
    return jsonable(forecast_backtest_summary())


@app.get("/models/drift")
def get_model_drift() -> dict[str, Any]:
    return jsonable(drift_summary(active_events()))


@app.get("/models/retrain-plan")
def get_model_retrain_plan() -> dict[str, Any]:
    return retrain_plan()


_RETRAIN_LOCK = threading.Lock()
_RETRAIN_STATE: dict[str, Any] = {
    "status": "idle",
    "started_at": None,
    "finished_at": None,
    "result": None,
    "error": None,
}


def _run_retrain_job(use_feedback: bool) -> None:
    """Retrain artifacts from events + feedback, then hot-reload them. Runs in a thread."""
    from backend.ml.predict import reload_artifacts
    from backend.ml.train_models import run_training
    from backend.monitoring.operational_monitoring import prediction_accuracy_metrics

    try:
        accuracy_before = prediction_accuracy_metrics()
        summary = run_training(use_feedback=use_feedback)
        reload_artifacts()
        _RETRAIN_STATE.update(
            status="completed",
            finished_at=datetime.now(UTC).isoformat(),
            result={**summary, "feedback_accuracy_before": accuracy_before},
            error=None,
        )
    except Exception as exc:  # pragma: no cover - background diagnostics
        _RETRAIN_STATE.update(
            status="failed",
            finished_at=datetime.now(UTC).isoformat(),
            error=str(exc),
        )
    finally:
        _RETRAIN_LOCK.release()


@app.post("/models/retrain")
def post_model_retrain(
    use_feedback: bool = True,
    context: RequestContext = Depends(require_roles("admin")),
) -> dict[str, Any]:
    """Kick off a background retrain that learns from accumulated officer feedback."""
    if not _RETRAIN_LOCK.acquire(blocking=False):
        return {
            "status": "already_running",
            "detail": "A retrain job is already in progress.",
            "state": jsonable(_RETRAIN_STATE),
        }
    _RETRAIN_STATE.update(
        status="running",
        started_at=datetime.now(UTC).isoformat(),
        finished_at=None,
        result=None,
        error=None,
    )
    audit_log(
        "models.retrain",
        context.user_id,
        context.tenant_id,
        "models",
        "retrain",
        {"use_feedback": use_feedback},
    )
    threading.Thread(target=_run_retrain_job, args=(use_feedback,), daemon=True).start()
    return {"status": "started", "state": jsonable(_RETRAIN_STATE)}


@app.get("/models/retrain/status")
def get_model_retrain_status() -> dict[str, Any]:
    return jsonable(_RETRAIN_STATE)


@app.post("/events/{event_id}/forecast")
def post_forecast(event_id: str) -> dict[str, Any]:
    return forecast_event(event_id)


@app.post("/events/{event_id}/plan")
def post_plan(event_id: str) -> dict[str, Any]:
    return plan_event(event_id)


@app.post("/plans/multi-incident")
def post_multi_incident_plan(payload: MultiIncidentPlanRequest | None = None) -> dict[str, Any]:
    request = payload or MultiIncidentPlanRequest()
    if request.event_ids:
        events = [get_event_or_404(event_id) for event_id in request.event_ids]
    else:
        events = active_events()
    return jsonable(build_multi_incident_plan(events, request.scenarios))


@app.post("/workflow/plans")
def post_workflow_plan(payload: PlanWorkflowRequest) -> dict[str, Any]:
    get_event_or_404(payload.event_id)
    return jsonable(create_plan_record(payload.event_id, payload.plan, payload.actor, payload.tenant_id))


@app.post("/workflow/plans/{plan_id}/approval")
def post_workflow_approval(plan_id: str, payload: ApprovalRequest) -> dict[str, Any]:
    updated = update_plan_approval(
        plan_id,
        payload.action,
        payload.actor,
        payload.tenant_id,
        payload.comment,
    )
    if updated is None:
        raise HTTPException(status_code=404, detail=f"Unknown plan_id: {plan_id}")
    return jsonable(updated)


@app.get("/workflow/plans/{plan_id}/history")
def get_workflow_plan_history(plan_id: str) -> list[dict[str, Any]]:
    history = plan_history(plan_id)
    if not history:
        raise HTTPException(status_code=404, detail=f"Unknown plan_id: {plan_id}")
    return jsonable(history)


@app.get("/reports/after-action/{event_id}")
def get_after_action_report(event_id: str) -> dict[str, Any]:
    get_event_or_404(event_id)
    return jsonable(after_action_report(event_id))


@app.get("/reports/after-action/{event_id}/csv")
def get_after_action_report_csv(event_id: str) -> Response:
    get_event_or_404(event_id)
    return Response(
        content=after_action_csv(event_id),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{event_id}-after-action.csv"'},
    )


@app.get("/sla/events/{event_id}")
def get_event_sla(event_id: str) -> dict[str, Any]:
    get_event_or_404(event_id)
    return jsonable(sla_summary(event_id))


@app.get("/audit/log")
def get_audit_log(limit: int = 50) -> list[dict[str, Any]]:
    rows = all_audit_logs()
    return jsonable(rows[:max(1, min(limit, 500))])


@app.post("/events/{event_id}/feedback")
def post_feedback(event_id: str, payload: FeedbackRequest) -> dict[str, Any]:
    return write_feedback(event_id, payload)


@app.get("/metrics/summary")
def get_metrics_summary() -> dict[str, Any]:
    return metrics_summary()


@app.get("/metrics/roi")
def get_metrics_roi() -> dict[str, Any]:
    planned = [planned_event_response(event) for event in [*load_planned_events(), *planned_permits()]]
    return jsonable(executive_roi_summary(active_events(), planned, feedback_rows_for_roi()))


@app.get("/metrics/operational")
def get_metrics_operational(event_id: str | None = None) -> dict[str, Any]:
    plan = None
    if event_id:
        event = get_event_or_404(event_id)
        forecast = predict_impact(event)
        merged = dict(event)
        merged.update(forecast)
        plan = generate_deployment_plan(merged)
    return jsonable(operational_metrics_snapshot(plan))


@app.get("/field/assignments")
def get_field_assignments(station: str = "Cubbon Park") -> dict[str, Any]:
    return jsonable(field_assignments(station))


@app.post("/field/status")
def post_field_status(payload: FieldStatusRequest) -> dict[str, Any]:
    get_event_or_404(payload.event_id)
    return jsonable(
        record_field_status(
            station=payload.station,
            event_id=payload.event_id,
            control_point_node_id=payload.control_point_node_id,
            status=payload.status,
            actor=payload.actor,
            tenant_id=payload.tenant_id,
            lat=payload.lat,
            lon=payload.lon,
            note=payload.note,
            photo_url=payload.photo_url,
        )
    )


@app.get("/field/status")
def get_field_status(event_id: str | None = None, station: str | None = None) -> list[dict[str, Any]]:
    rows = all_field_status_rows()
    if event_id is not None:
        rows = [row for row in rows if str(row.get("event_id")) == str(event_id)]
    if station is not None:
        rows = [
            row
            for row in rows
            if str(row.get("station", "")).strip().lower() == station.strip().lower()
        ]
    rows.sort(key=lambda row: str(row.get("created_at", "")), reverse=True)
    return jsonable(rows[:100])


@app.websocket("/ws/live")
async def websocket_live(websocket: WebSocket) -> None:
    await websocket.accept()
    previous_active_ids: set[str] = set()
    try:
        while True:
            try:
                events = active_events()
                active_ids = {str(event["id"]) for event in events}
                newly_active = [
                    event for event in events if str(event["id"]) not in previous_active_ids
                ]
                await websocket.send_json(
                    {
                        "metrics": metrics_summary(),
                        "newly_active_events": newly_active,
                        "sent_at": datetime.now(UTC).isoformat(),
                    }
                )
                previous_active_ids = active_ids
            except WebSocketDisconnect:
                raise
            except Exception as exc:  # noqa: BLE001 - keep the socket alive on transient errors
                print(f"ws/live: skipped a tick due to backend error ({exc})", flush=True)
            await asyncio.sleep(5)
    except WebSocketDisconnect:
        return


if FRONTEND_DIST.is_dir():
    assets_dir = FRONTEND_DIST / "assets"
    if assets_dir.is_dir():
        app.mount("/assets", StaticFiles(directory=assets_dir), name="frontend-assets")

    @app.get("/app/{full_path:path}")
    def serve_frontend_app(full_path: str = "") -> FileResponse:
        candidate = FRONTEND_DIST / full_path
        if full_path and candidate.is_file():
            return FileResponse(candidate)
        # A request for a concrete asset (anything with a file extension, e.g.
        # /app/assets/index-*.js) that does not exist must 404 rather than fall
        # back to index.html -- otherwise the browser receives HTML for a
        # module script and fails with a confusing MIME-type error.
        if Path(full_path).suffix:
            raise HTTPException(status_code=404, detail=f"Asset not found: {full_path}")
        # SPA client-side route -> serve the app shell.
        return FileResponse(FRONTEND_DIST / "index.html")

    @app.get("/app")
    def serve_frontend_root() -> FileResponse:
        return FileResponse(FRONTEND_DIST / "index.html")
