from __future__ import annotations

import json
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from backend.optimization.allocation import load_police_stations
from backend.data.workflow import feedback_rows as get_feedback_rows, all_field_status_rows


def _parse_created_at(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def recent_feedback(days: int = 30, rows: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    cutoff = datetime.now(UTC) - timedelta(days=days)
    source_rows = rows if rows is not None else get_feedback_rows()
    recent_rows = []
    for row in source_rows:
        created = _parse_created_at(row.get("created_at"))
        if created is None:
            continue
        if created >= cutoff:
            recent_rows.append(row)
    return recent_rows


def safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def forecast_error_by_category(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, list[float]] = defaultdict(list)
    for row in rows:
        predicted = safe_float(row.get("predicted_duration_minutes"))
        actual = safe_float(row.get("actual_duration_minutes"))
        if predicted <= 0 or actual <= 0:
            continue
        category = str(row.get("event_name") or row.get("predicted_severity") or "unknown")
        grouped[category].append(abs(predicted - actual) / actual * 100.0)

    return [
        {
            "category": category,
            "mape": round(sum(errors) / len(errors), 2),
            "sample_count": len(errors),
        }
        for category, errors in sorted(grouped.items(), key=lambda item: (-len(item[1]), item[0]))[:8]
    ]


def active_high_risk_corridors(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    corridors = {}
    for event in events:
        event_risk = safe_float(event.get("risk_score"), default=-1.0)
        if event_risk < 0:
            priority = str(event.get("priority") or "").strip().lower()
            cause = str(event.get("event_cause") or "").strip().lower()
            corridor_value = str(event.get("corridor") or "").strip().lower()
            event_risk = 0.0
            if priority == "high":
                event_risk += 0.48
            if corridor_value and corridor_value not in {"non-corridor", "unknown", "none"}:
                event_risk += 0.18
            if any(token in cause for token in ("accident", "water", "construction", "vip", "protest", "procession")):
                event_risk += 0.16
        event_risk = min(max(event_risk, 0.0), 1.0)
        if event_risk <= 0.7:
            continue
        corridor = str(event.get("corridor") or "Unknown")
        current = corridors.setdefault(
            corridor,
            {
                "corridor": corridor,
                "risk_score": 0.0,
                "event_count": 0,
                "preventable": True,
            },
        )
        current["risk_score"] = max(current["risk_score"], round(event_risk, 3))
        current["event_count"] += 1
    return sorted(corridors.values(), key=lambda row: row["risk_score"], reverse=True)


def executive_roi_summary(
    active_events: list[dict[str, Any]],
    planned_events: list[dict[str, Any]],
    feedback_rows: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    rows = recent_feedback(rows=feedback_rows)
    accepted_rows = [row for row in rows if row.get("plan_accepted") is True]
    predicted_actual_pairs = [
        (
            safe_float(row.get("predicted_duration_minutes")),
            safe_float(row.get("actual_duration_minutes")),
        )
        for row in rows
        if safe_float(row.get("predicted_duration_minutes")) > 0
        and safe_float(row.get("actual_duration_minutes")) > 0
    ]
    reductions = [
        max(predicted - actual, 0.0)
        for predicted, actual in predicted_actual_pairs
    ]
    estimated_reductions = [
        min(safe_float(row.get("predicted_duration_minutes")) * 0.18, 45.0)
        for row in accepted_rows
        if safe_float(row.get("predicted_duration_minutes")) > 0
        and safe_float(row.get("actual_duration_minutes")) <= 0
    ]
    roi_reductions = [*reductions, *estimated_reductions]
    average_duration_reduction = (
        sum(roi_reductions) / len(roi_reductions)
        if roi_reductions
        else 0.0
    )
    plan_acceptance_rate = len(accepted_rows) / len(rows) if rows else 0.0
    deployed_personnel = sum(
        int(row.get("adjusted_personnel") or row.get("plan_total_personnel") or 0)
        for row in accepted_rows
    )
    station_capacity = sum(int(station.get("available_personnel") or 0) for station in load_police_stations())
    personnel_utilization = deployed_personnel / max(station_capacity, 1)
    high_risk_corridors = active_high_risk_corridors([*active_events, *planned_events])
    delay_hours_avoided = sum(roi_reductions) * 110.0 / 60.0
    field_status_rows = all_field_status_rows()
    closure_updates = [
        row for row in field_status_rows if row.get("status") in {"road_cleared", "Road cleared"}
    ]
    closure_compliance_rate = len(closure_updates) / max(len(accepted_rows), 1) if accepted_rows else 0.0

    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "average_incident_duration_reduction_minutes": round(average_duration_reduction, 1),
        "deployment_time_saved_minutes": round(len(accepted_rows) * 12.0, 1),
        "personnel_utilization": round(min(personnel_utilization, 1.0), 3),
        "preventable_high_risk_corridors_detected": len(high_risk_corridors),
        "high_risk_corridors": high_risk_corridors[:6],
        "forecast_error_by_event_category": forecast_error_by_category(rows),
        "plan_acceptance_rate": round(plan_acceptance_rate, 3),
        "citizen_delay_hours_avoided": round(delay_hours_avoided, 1),
        "closure_compliance_rate": round(min(closure_compliance_rate, 1.0), 3),
        "average_reopening_time_minutes": None,
        "sample_count": len(rows),
        "method_notes": [
            "Duration reduction uses actual outcomes when available and a conservative 18% pilot estimate for accepted plans awaiting actual duration.",
            "Delay-hours avoided estimates 110 affected road users per reduced incident minute.",
            "Deployment time saved uses a conservative 12 minute saving per accepted plan.",
        ],
    }
