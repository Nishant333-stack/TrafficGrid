from __future__ import annotations

import csv
import io
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from sqlalchemy import text

from backend.config.paths import PROJECT_ROOT
from backend.config.db import get_engine

APP_ROOT = PROJECT_ROOT


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def audit_log(
    action: str,
    actor: str,
    tenant_id: str,
    resource_type: str,
    resource_id: str,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    row = {
        "audit_id": str(uuid4()),
        "created_at": utc_now(),
        "tenant_id": tenant_id,
        "actor": actor,
        "action": action,
        "resource_type": resource_type,
        "resource_id": resource_id,
        "details": details or {},
    }
    
    engine = get_engine()
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                INSERT INTO audit_log (audit_id, tenant_id, actor, action, resource_type, resource_id, details, created_at)
                VALUES (:audit_id, :tenant_id, :actor, :action, :resource_type, :resource_id, CAST(:details AS JSONB), :created_at)
                """
            ),
            {
                "audit_id": row["audit_id"],
                "tenant_id": row["tenant_id"],
                "actor": row["actor"],
                "action": row["action"],
                "resource_type": row["resource_type"],
                "resource_id": row["resource_id"],
                "details": json.dumps(row["details"]),
                "created_at": row["created_at"],
            },
        )
        
        
    return row


def all_audit_logs() -> list[dict[str, Any]]:
    engine = get_engine()
    with engine.connect() as conn:
        result = conn.execute(
            text("SELECT * FROM audit_log ORDER BY created_at DESC")
        )
        rows = []
        for r in result:
            rows.append({
                "audit_id": r.audit_id,
                "tenant_id": r.tenant_id,
                "actor": r.actor,
                "action": r.action,
                "resource_type": r.resource_type,
                "resource_id": r.resource_id,
                "details": r.details,
                "created_at": r.created_at.isoformat() if r.created_at else None,
            })
        return rows

def create_plan_record(
    event_id: str,
    plan: dict[str, Any],
    actor: str,
    tenant_id: str,
) -> dict[str, Any]:
    plan_id = str(uuid4())
    row = {
        "record_type": "plan_version",
        "plan_id": plan_id,
        "event_id": event_id,
        "version": 1,
        "status": "draft",
        "tenant_id": tenant_id,
        "created_at": utc_now(),
        "actor": actor,
        "approval_chain": [
            {"role": "traffic_commander", "status": "pending"},
            {"role": "zone_superintendent", "status": "pending"},
        ],
        "plan": plan,
        "comment": "Plan created",
    }
    
    engine = get_engine()
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                INSERT INTO plan_workflows (plan_id, event_id, version, status, tenant_id, actor, approval_chain, plan_json, comment, created_at)
                VALUES (:plan_id, :event_id, :version, :status, :tenant_id, :actor, CAST(:approval_chain AS JSONB), CAST(:plan_json AS JSONB), :comment, :created_at)
                """
            ),
            {
                "plan_id": row["plan_id"],
                "event_id": row["event_id"],
                "version": row["version"],
                "status": row["status"],
                "tenant_id": row["tenant_id"],
                "actor": row["actor"],
                "approval_chain": json.dumps(row["approval_chain"]),
                "plan_json": json.dumps(row["plan"]),
                "comment": row["comment"],
                "created_at": row["created_at"],
            },
        )
    audit_log("plan.created", actor, tenant_id, "plan", plan_id, {"event_id": event_id})
    return row


def plan_history(plan_id: str) -> list[dict[str, Any]]:
    engine = get_engine()
    with engine.connect() as conn:
        result = conn.execute(
            text("SELECT * FROM plan_workflows WHERE plan_id = :plan_id ORDER BY version ASC"),
            {"plan_id": plan_id}
        )
        rows = []
        for r in result:
            rows.append({
                "plan_id": r.plan_id,
                "event_id": r.event_id,
                "version": r.version,
                "status": r.status,
                "tenant_id": r.tenant_id,
                "actor": r.actor,
                "approval_chain": r.approval_chain,
                "plan": r.plan_json,
                "comment": r.comment,
                "created_at": r.created_at.isoformat() if r.created_at else None,
            })
        return rows


def latest_plan_version(plan_id: str) -> dict[str, Any] | None:
    history = plan_history(plan_id)
    if not history:
        return None
    return max(history, key=lambda row: int(row.get("version") or 0))


def update_plan_approval(
    plan_id: str,
    action: str,
    actor: str,
    tenant_id: str,
    comment: str | None = None,
) -> dict[str, Any] | None:
    latest = latest_plan_version(plan_id)
    if latest is None:
        return None

    status_map = {
        "submit": "submitted",
        "approve": "approved",
        "reject": "rejected",
        "activate": "active",
        "close": "closed",
    }
    next_status = status_map.get(action, action)
    next_row = dict(latest)
    next_row["version"] = int(latest.get("version") or 0) + 1
    next_row["status"] = next_status
    next_row["created_at"] = utc_now()
    next_row["actor"] = actor
    next_row["comment"] = comment or f"Plan {next_status}"
    chain = []
    for step in latest.get("approval_chain", []):
        step_copy = dict(step)
        if next_status == "approved" and step_copy.get("status") == "pending":
            step_copy["status"] = "approved"
            step_copy["actor"] = actor
            step_copy["approved_at"] = next_row["created_at"]
            chain.append(step_copy)
            chain.extend(latest.get("approval_chain", [])[len(chain):])
            break
        chain.append(step_copy)
    next_row["approval_chain"] = chain or latest.get("approval_chain", [])
    
    engine = get_engine()
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                INSERT INTO plan_workflows (plan_id, event_id, version, status, tenant_id, actor, approval_chain, plan_json, comment, created_at)
                VALUES (:plan_id, :event_id, :version, :status, :tenant_id, :actor, CAST(:approval_chain AS JSONB), CAST(:plan_json AS JSONB), :comment, :created_at)
                """
            ),
            {
                "plan_id": next_row["plan_id"],
                "event_id": next_row["event_id"],
                "version": next_row["version"],
                "status": next_row["status"],
                "tenant_id": next_row["tenant_id"],
                "actor": next_row["actor"],
                "approval_chain": json.dumps(next_row["approval_chain"]),
                "plan_json": json.dumps(next_row["plan"]),
                "comment": next_row["comment"],
                "created_at": next_row["created_at"],
            },
        )
    audit_log(
        f"plan.{next_status}",
        actor,
        tenant_id,
        "plan",
        plan_id,
        {"event_id": latest.get("event_id"), "comment": comment},
    )
    return next_row


def record_field_status(
    station: str,
    event_id: str,
    control_point_node_id: Any,
    status: str,
    actor: str,
    tenant_id: str,
    lat: float | None = None,
    lon: float | None = None,
    note: str | None = None,
    photo_url: str | None = None,
) -> dict[str, Any]:
    row = {
        "status_id": str(uuid4()),
        "created_at": utc_now(),
        "tenant_id": tenant_id,
        "actor": actor,
        "station": station,
        "event_id": event_id,
        "control_point_node_id": control_point_node_id,
        "status": status,
        "lat": lat,
        "lon": lon,
        "note": note,
        "photo_url": photo_url,
    }
    
    engine = get_engine()
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                INSERT INTO field_status_updates (status_id, tenant_id, actor, station, event_id, control_point_node_id, status, latitude, longitude, note, photo_url, created_at)
                VALUES (:status_id, :tenant_id, :actor, :station, :event_id, :control_point_node_id, :status, :lat, :lon, :note, :photo_url, :created_at)
                """
            ),
            {
                "status_id": row["status_id"],
                "tenant_id": row["tenant_id"],
                "actor": row["actor"],
                "station": row["station"],
                "event_id": row["event_id"],
                "control_point_node_id": str(row["control_point_node_id"]) if row["control_point_node_id"] else None,
                "status": row["status"],
                "lat": row["lat"],
                "lon": row["lon"],
                "note": row["note"],
                "photo_url": row["photo_url"],
                "created_at": row["created_at"],
            },
        )
    audit_log(
        "field.status",
        actor,
        tenant_id,
        "control_point",
        str(control_point_node_id),
        {"event_id": event_id, "status": status},
    )
    return row


def feedback_rows() -> list[dict[str, Any]]:
    engine = get_engine()
    with engine.connect() as conn:
        result = conn.execute(text("SELECT * FROM feedback ORDER BY created_at ASC"))
        rows = []
        for r in result:
            rows.append({
                "id": r.id,
                "event_id": r.event_id,
                "predicted_severity": r.predicted_severity,
                "predicted_duration_minutes": r.predicted_duration_minutes,
                "actual_duration_minutes": r.actual_duration_minutes,
                "officer_rating": r.officer_rating,
                "plan_accepted": r.plan_accepted,
                "adjusted_personnel": r.adjusted_personnel,
                "plan_total_personnel": r.plan_total_personnel,
                "plan_json": r.plan_json,
                "seed_source": r.seed_source,
                "event_name": r.event_name,
                "created_at": r.created_at.isoformat() if r.created_at else None,
            })
        return rows


def _get_workflow_rows(event_id: str) -> list[dict[str, Any]]:
    engine = get_engine()
    with engine.connect() as conn:
        result = conn.execute(
            text("SELECT * FROM plan_workflows WHERE event_id = :event_id ORDER BY version ASC"),
            {"event_id": event_id}
        )
        rows = []
        for r in result:
            rows.append({
                "plan_id": r.plan_id,
                "event_id": r.event_id,
                "version": r.version,
                "status": r.status,
                "tenant_id": r.tenant_id,
                "actor": r.actor,
                "approval_chain": r.approval_chain,
                "plan": r.plan_json,
                "comment": r.comment,
                "created_at": r.created_at.isoformat() if r.created_at else None,
            })
        return rows
    
def _get_status_rows(event_id: str) -> list[dict[str, Any]]:
    engine = get_engine()
    with engine.connect() as conn:
        result = conn.execute(
            text("SELECT * FROM field_status_updates WHERE event_id = :event_id ORDER BY created_at ASC"),
            {"event_id": event_id}
        )
        rows = []
        for r in result:
            rows.append({
                "status_id": r.status_id,
                "tenant_id": r.tenant_id,
                "actor": r.actor,
                "station": r.station,
                "event_id": r.event_id,
                "control_point_node_id": r.control_point_node_id,
                "status": r.status,
                "lat": r.latitude,
                "lon": r.longitude,
                "note": r.note,
                "photo_url": r.photo_url,
                "created_at": r.created_at.isoformat() if r.created_at else None,
            })
        return rows


def all_field_status_rows() -> list[dict[str, Any]]:
    engine = get_engine()
    with engine.connect() as conn:
        result = conn.execute(
            text("SELECT * FROM field_status_updates ORDER BY created_at ASC")
        )
        rows = []
        for r in result:
            rows.append({
                "status_id": r.status_id,
                "tenant_id": r.tenant_id,
                "actor": r.actor,
                "station": r.station,
                "event_id": r.event_id,
                "control_point_node_id": r.control_point_node_id,
                "status": r.status,
                "lat": r.latitude,
                "lon": r.longitude,
                "note": r.note,
                "photo_url": r.photo_url,
                "created_at": r.created_at.isoformat() if r.created_at else None,
            })
        return rows


def sla_summary(event_id: str) -> dict[str, Any]:
    versions = _get_workflow_rows(event_id)
    statuses = _get_status_rows(event_id)
    if not versions:
        return {
            "event_id": event_id,
            "status": "no_plan_record",
            "time_to_assign_minutes": None,
            "time_to_deploy_minutes": None,
            "time_to_resolve_minutes": None,
        }

    first = min(versions, key=lambda row: row.get("created_at", ""))
    approved = next((row for row in versions if row.get("status") == "approved"), None)
    deployed = next((row for row in statuses if row.get("status") in {"deployed", "Deployed"}), None)
    resolved = next((row for row in statuses if row.get("status") in {"road_cleared", "Road cleared"}), None)

    def minutes_between(start: str | None, end: str | None) -> float | None:
        if not start or not end:
            return None
        start_dt = datetime.fromisoformat(start.replace("Z", "+00:00"))
        end_dt = datetime.fromisoformat(end.replace("Z", "+00:00"))
        return round((end_dt - start_dt).total_seconds() / 60.0, 1)

    return {
        "event_id": event_id,
        "status": "ok",
        "time_to_assign_minutes": minutes_between(first.get("created_at"), approved.get("created_at") if approved else None),
        "time_to_deploy_minutes": minutes_between(first.get("created_at"), deployed.get("created_at") if deployed else None),
        "time_to_resolve_minutes": minutes_between(first.get("created_at"), resolved.get("created_at") if resolved else None),
        "field_updates": len(statuses),
        "plan_versions": len(versions),
    }


def after_action_report(event_id: str) -> dict[str, Any]:
    plans = _get_workflow_rows(event_id)
    feedback = [
        row
        for row in feedback_rows()
        if str(row.get("event_id")) == str(event_id)
    ]
    statuses = _get_status_rows(event_id)
    latest_plan = max(plans, key=lambda row: int(row.get("version") or 0), default=None)
    return {
        "event_id": event_id,
        "generated_at": utc_now(),
        "latest_plan_status": latest_plan.get("status") if latest_plan else None,
        "plan_versions": len(plans),
        "feedback_count": len(feedback),
        "field_update_count": len(statuses),
        "officer_acknowledgements": [
            row for row in statuses if row.get("status") in {"acknowledged", "deployed", "Deployed"}
        ],
        "sla": sla_summary(event_id),
        "latest_plan": latest_plan,
        "feedback": feedback[-10:],
    }


def after_action_csv(event_id: str) -> str:
    report = after_action_report(event_id)
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["metric", "value"])
    writer.writerow(["event_id", report["event_id"]])
    writer.writerow(["generated_at", report["generated_at"]])
    writer.writerow(["latest_plan_status", report["latest_plan_status"]])
    writer.writerow(["plan_versions", report["plan_versions"]])
    writer.writerow(["feedback_count", report["feedback_count"]])
    writer.writerow(["field_update_count", report["field_update_count"]])
    for key, value in report["sla"].items():
        writer.writerow([f"sla_{key}", value])
    return buffer.getvalue()
