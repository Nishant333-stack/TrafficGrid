from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from backend.config.env_loader import load_project_env
from backend.config.paths import PROJECT_ROOT


load_project_env()


def model_dir() -> Path:
    """Resolve the active model directory the same way predict.py does."""
    raw = os.environ.get("MODEL_DIR")
    return Path(raw) if raw else (PROJECT_ROOT / "models")


def artifact_exists(path: Path) -> bool:
    return path.exists()


def platform_health(database_connected: bool, integration_count: int) -> dict[str, Any]:
    database_configured = bool(os.environ.get("DATABASE_URL"))
    models = model_dir()
    # The demo/full road graph caches live next to the road_graph module.
    from backend.geo.road_graph import DEFAULT_CACHE_PATH, DEMO_CACHE_PATH

    artifacts = {
        "severity_model": artifact_exists(models / "severity_model.pkl"),
        "duration_q25_model": artifact_exists(models / "duration_q25_model.pkl"),
        "duration_q50_model": artifact_exists(models / "duration_q50_model.pkl"),
        "duration_q75_model": artifact_exists(models / "duration_q75_model.pkl"),
        "risk_density": artifact_exists(models / "risk_density.parquet"),
        "road_graph_cache": artifact_exists(DEFAULT_CACHE_PATH) or artifact_exists(DEMO_CACHE_PATH),
    }
    healthy = all(artifacts.values()) and (database_connected or not database_configured)
    return {
        "status": "ok" if healthy else "degraded",
        "generated_at": datetime.now(UTC).isoformat(),
        "environment": os.environ.get("APP_ENV", "local"),
        "database_configured": database_configured,
        "database_connected": database_connected,
        "integrations_online": integration_count,
        "artifacts": artifacts,
        "backup_target_configured": bool(os.environ.get("BACKUP_TARGET_URL")),
    }


def retention_policy() -> dict[str, Any]:
    return {
        "event_history_days": int(os.environ.get("RETENTION_EVENT_HISTORY_DAYS", "365")),
        "audit_log_days": int(os.environ.get("RETENTION_AUDIT_LOG_DAYS", "730")),
        "field_location_days": int(os.environ.get("RETENTION_FIELD_LOCATION_DAYS", "30")),
        "model_artifact_versions": int(os.environ.get("RETENTION_MODEL_ARTIFACT_VERSIONS", "5")),
        "mode": "policy_only_local_demo",
    }


def retention_dry_run() -> dict[str, Any]:
    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "dry_run": True,
        "deleted_rows": 0,
        "deleted_files": 0,
        "policy": retention_policy(),
        "note": "Production mode should run this against tenant-scoped database partitions and object storage.",
    }


def security_controls() -> dict[str, Any]:
    return {
        "tenant_isolation": "X-Tenant-ID request context with tenant-scoped records",
        "rbac": {
            "admin": ["all"],
            "traffic_commander": ["forecast", "plan", "approve", "reports"],
            "field_officer": ["field_status", "assignments"],
            "executive": ["metrics", "roi", "reports"],
        },
        "sso_saml": {
            "enabled": os.environ.get("SSO_ENABLED", "false").lower() == "true",
            "metadata_url_configured": bool(os.environ.get("SAML_METADATA_URL")),
        },
        "audit": "append-only audit_log.jsonl locally; audit_log table in Postgres schema",
        "secrets": "DATABASE_URL and SSO settings are environment variables",
        "location_data": "field GPS retention is controlled separately from incident history",
    }
