from __future__ import annotations

import csv
import io
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4


from backend.config.paths import PROJECT_ROOT


APP_ROOT = PROJECT_ROOT
AUDIT_LOG_PATH = PROJECT_ROOT / "audit_log.jsonl"
PLAN_WORKFLOW_PATH = PROJECT_ROOT / "plan_workflows.jsonl"
FIELD_STATUS_PATH = PROJECT_ROOT / "field_status_log.jsonl"
LOCAL_FEEDBACK_PATH = PROJECT_ROOT / "feedback_log.jsonl"


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return rows


def append_jsonl(path: Path, row: dict[str, Any]) -> None:
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, default=str) + "\n")


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
    append_jsonl(AUDIT_LOG_PATH, row)
    return row


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
    append_jsonl(PLAN_WORKFLOW_PATH, row)
    audit_log("plan.created", actor, tenant_id, "plan", plan_id, {"event_id": event_id})
    return row


def plan_history(plan_id: str) -> list[dict[str, Any]]:
    return [
        row
        for row in read_jsonl(PLAN_WORKFLOW_PATH)
        if row.get("plan_id") == plan_id
    ]


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
    append_jsonl(PLAN_WORKFLOW_PATH, next_row)
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
    append_jsonl(FIELD_STATUS_PATH, row)
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
    return read_jsonl(LOCAL_FEEDBACK_PATH)


def sla_summary(event_id: str) -> dict[str, Any]:
    versions = [
        row
        for row in read_jsonl(PLAN_WORKFLOW_PATH)
        if str(row.get("event_id")) == str(event_id)
    ]
    statuses = [
        row
        for row in read_jsonl(FIELD_STATUS_PATH)
        if str(row.get("event_id")) == str(event_id)
    ]
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
    plans = [
        row
        for row in read_jsonl(PLAN_WORKFLOW_PATH)
        if str(row.get("event_id")) == str(event_id)
    ]
    feedback = [
        row
        for row in feedback_rows()
        if str(row.get("event_id")) == str(event_id)
    ]
    statuses = [
        row
        for row in read_jsonl(FIELD_STATUS_PATH)
        if str(row.get("event_id")) == str(event_id)
    ]
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
