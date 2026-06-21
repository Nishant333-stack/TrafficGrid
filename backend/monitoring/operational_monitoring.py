from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pandas as pd

from backend.optimization.allocation import effective_barricades, effective_personnel, load_police_stations
from backend.geo.road_graph import graph_cache_metrics


from backend.config.paths import PROJECT_ROOT


APP_ROOT = PROJECT_ROOT
TRAINING_CACHE_PATH = PROJECT_ROOT / "backend" / "ml" / "models" / "training_events_preprocessed.parquet"
MAX_REASONABLE_DURATION_MINUTES = 24 * 60


def safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def read_feedback_rows() -> list[dict[str, Any]]:
    from backend.data.workflow import feedback_rows
    return feedback_rows()


def prediction_accuracy_metrics(rows: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    rows = rows if rows is not None else read_feedback_rows()
    errors: list[float] = []
    squared_errors: list[float] = []
    percentage_errors: list[float] = []
    for row in rows:
        predicted = safe_float(row.get("predicted_duration_minutes"), -1.0)
        actual = safe_float(row.get("actual_duration_minutes"), -1.0)
        if predicted <= 0 or actual <= 0:
            continue
        error = predicted - actual
        errors.append(abs(error))
        squared_errors.append(error * error)
        percentage_errors.append(abs(error) / actual)

    if not errors:
        return {
            "sample_count": 0,
            "mae_minutes": None,
            "rmse_minutes": None,
            "mape": None,
        }

    return {
        "sample_count": len(errors),
        "mae_minutes": round(sum(errors) / len(errors), 2),
        "rmse_minutes": round((sum(squared_errors) / len(squared_errors)) ** 0.5, 2),
        "mape": round(sum(percentage_errors) / len(percentage_errors), 4),
    }


def route_quality_metrics(plan: dict[str, Any] | None = None) -> dict[str, Any]:
    diversions = list((plan or {}).get("diversions") or [])
    if not diversions:
        return {
            "route_count": 0,
            "route_quality_score": None,
            "average_travel_time_reduction": None,
            "max_corridor_risk": None,
        }

    quality_scores = [safe_float(row.get("route_quality_score")) for row in diversions]
    cost_deltas = [safe_float(row.get("congestion_cost_delta")) for row in diversions]
    original_costs = [max(safe_float(row.get("original_congestion_cost")), 1.0) for row in diversions]
    max_risks = [safe_float(row.get("max_corridor_risk")) for row in diversions]
    reductions = [
        max(0.0, delta) / original
        for delta, original in zip(cost_deltas, original_costs)
    ]
    return {
        "route_count": len(diversions),
        "route_quality_score": round(sum(quality_scores) / len(quality_scores), 3),
        "average_travel_time_reduction": round(sum(reductions) / len(reductions), 3),
        "max_corridor_risk": round(max(max_risks), 3),
    }


def resource_quality_metrics(plan: dict[str, Any] | None = None) -> dict[str, Any]:
    plan = plan or {}
    stations = load_police_stations()
    shift_personnel = sum(effective_personnel(station) for station in stations)
    barricade_inventory = sum(effective_barricades(station) for station in stations)
    assigned_personnel = sum(
        int(row.get("personnel_assigned") or 0)
        for row in plan.get("allocations", [])
    )
    assigned_barricades = sum(
        int(row.get("barricades_assigned") or 0)
        for row in plan.get("barricade_allocations", [])
    )
    required_personnel = int(plan.get("total_personnel") or 0)
    required_barricades = int(plan.get("total_barricades") or 0)

    return {
        "personnel_utilization": round(assigned_personnel / max(shift_personnel, 1), 3),
        "personnel_fulfillment": round(assigned_personnel / max(required_personnel, 1), 3)
        if required_personnel
        else None,
        "personnel_shortfall": int(plan.get("personnel_shortfall") or plan.get("shortfall") or 0),
        "barricade_effectiveness": round(assigned_barricades / max(required_barricades, 1), 3)
        if required_barricades
        else None,
        "barricade_inventory_utilization": round(assigned_barricades / max(barricade_inventory, 1), 3),
        "barricade_shortfall": int(plan.get("barricade_shortfall") or 0),
    }


def training_data_quality() -> dict[str, Any]:
    if not TRAINING_CACHE_PATH.exists():
        return {
            "status": "missing_training_cache",
            "critical_warnings": ["training_events_preprocessed.parquet is missing"],
        }

    frame = pd.read_parquet(TRAINING_CACHE_PATH)
    warnings: list[str] = []
    duration = pd.to_numeric(frame.get("duration_minutes"), errors="coerce")
    valid_duration = duration.notna() & (duration >= 0)
    outlier_count = int((duration > MAX_REASONABLE_DURATION_MINUTES).sum())
    if outlier_count:
        warnings.append(
            f"{outlier_count} duration rows exceed {MAX_REASONABLE_DURATION_MINUTES} minutes and can distort regressors"
        )
    closure = pd.to_numeric(frame.get("requires_road_closure"), errors="coerce")
    closure_rate = float(closure.mean(skipna=True) or 0.0)
    minority_share = min(closure_rate, 1.0 - closure_rate)
    if minority_share < 0.12:
        warnings.append("closure target is imbalanced; evaluate AUC/recall, not accuracy alone")

    return {
        "status": "ok",
        "rows": int(len(frame)),
        "duration_coverage": round(float(valid_duration.mean()), 3),
        "duration_outlier_count": outlier_count,
        "duration_p50_minutes": None if duration.dropna().empty else round(float(duration.dropna().median()), 2),
        "duration_p95_minutes": None if duration.dropna().empty else round(float(duration.dropna().quantile(0.95)), 2),
        "closure_positive_rate": round(closure_rate, 3),
        "critical_warnings": warnings,
    }


def operational_metrics_snapshot(plan: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "prediction_accuracy": prediction_accuracy_metrics(),
        "route_quality": route_quality_metrics(plan),
        "resource_quality": resource_quality_metrics(plan),
        "graph_cache": graph_cache_metrics(),
        "training_data_quality": training_data_quality(),
    }
