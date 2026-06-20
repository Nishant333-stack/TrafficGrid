from __future__ import annotations

import json
import os
import warnings
from functools import lru_cache
from pathlib import Path
from typing import Any

from backend.config.env_loader import load_project_env


load_project_env()

os.environ.setdefault("MPLCONFIGDIR", "/tmp/grid_matplotlib")
Path(os.environ["MPLCONFIGDIR"]).mkdir(parents=True, exist_ok=True)
warnings.filterwarnings(
    "ignore",
    message="X does not have valid feature names.*",
    category=UserWarning,
)

import joblib
import pandas as pd

from backend.ml.feature_cleaning import (
    duration_cap_for_event,
    event_category_for_cause,
    is_minor_road_defect_context,
    normalize_category,
    normalize_event_cause,
)
from backend.integrations.integrations import operational_context_for_event


MODEL_DIR = Path(os.environ.get("MODEL_DIR", Path(__file__).with_name("models")))
CATEGORICAL_FEATURES = [
    "event_type",
    "event_category",
    "event_cause",
    "corridor",
    "zone",
    "police_station",
    "priority",
    "veh_type",
]
SEVERITY_FEATURES = CATEGORICAL_FEATURES + ["hour_of_day", "day_of_week"]
DURATION_FEATURES = SEVERITY_FEATURES + ["requires_road_closure"]
HOUR_BUCKET_SIZE = 3


def bool_to_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass

    if isinstance(value, bool):
        return 1.0 if value else 0.0

    normalized = str(value).strip().lower()
    if normalized in {"true", "t", "yes", "y", "1"}:
        return 1.0
    if normalized in {"false", "f", "no", "n", "0"}:
        return 0.0
    return None


def hour_bucket(hour_of_day: int) -> int:
    if hour_of_day < 0:
        return -1
    return int(hour_of_day // HOUR_BUCKET_SIZE * HOUR_BUCKET_SIZE)


def time_parts(value: Any) -> tuple[int, int]:
    timestamp = pd.to_datetime(value, errors="coerce", utc=True)
    if pd.isna(timestamp):
        return -1, -1
    return int(timestamp.hour), int(timestamp.dayofweek)


def build_feature_frame(
    event_features: dict[str, Any],
    requires_road_closure: float | None = None,
) -> pd.DataFrame:
    hour_of_day, day_of_week = time_parts(event_features.get("start_datetime"))
    row: dict[str, Any] = {
        "hour_of_day": hour_of_day,
        "day_of_week": day_of_week,
    }

    cleaned_cause = normalize_event_cause(event_features.get("event_cause"))
    for column in CATEGORICAL_FEATURES:
        if column == "event_cause":
            row[column] = cleaned_cause
        elif column == "event_category":
            row[column] = event_category_for_cause(cleaned_cause)
        else:
            row[column] = normalize_category(event_features.get(column))

    if requires_road_closure is not None:
        row["requires_road_closure"] = requires_road_closure

    return pd.DataFrame([row])


@lru_cache(maxsize=1)
def load_artifacts() -> dict[str, Any]:
    model_dir = MODEL_DIR
    severity_path = model_dir / "severity_model.pkl"
    q25_path = model_dir / "duration_q25_model.pkl"
    q50_path = model_dir / "duration_q50_model.pkl"
    q75_path = model_dir / "duration_q75_model.pkl"
    risk_path = model_dir / "risk_density.parquet"

    missing = [
        path
        for path in [severity_path, q25_path, q50_path, q75_path, risk_path]
        if not path.exists()
    ]
    if missing:
        missing_list = ", ".join(str(path) for path in missing)
        raise FileNotFoundError(
            f"Missing model artifacts: {missing_list}. Run python train_models.py first."
        )

    return {
        "severity": joblib.load(severity_path),
        "duration": {
            "low": joblib.load(q25_path),
            "median": joblib.load(q50_path),
            "high": joblib.load(q75_path),
        },
        "risk_density": pd.read_parquet(risk_path),
        "survival": pd.read_parquet(model_dir / "duration_survival_table.parquet")
        if (model_dir / "duration_survival_table.parquet").exists()
        else pd.DataFrame(),
    }


def clamp(value: float, lower: float, upper: float) -> float:
    return max(lower, min(upper, value))


def risk_score_for_event(
    risk_density: pd.DataFrame,
    event_features: dict[str, Any],
) -> float:
    if risk_density.empty:
        return 0.0

    corridor = normalize_category(event_features.get("corridor"))
    hour_of_day, day_of_week = time_parts(event_features.get("start_datetime"))
    bucket = hour_bucket(hour_of_day)

    exact = risk_density.loc[
        (risk_density["corridor"] == corridor)
        & (risk_density["hour_bucket"] == bucket)
        & (risk_density["day_of_week"] == day_of_week)
    ]
    if not exact.empty:
        return clamp(float(exact["risk_score"].iloc[0]), 0.0, 1.0)

    corridor_rows = risk_density.loc[risk_density["corridor"] == corridor]
    if not corridor_rows.empty:
        return clamp(float(corridor_rows["risk_score"].mean()), 0.0, 1.0)

    return clamp(float(risk_density["risk_score"].mean()), 0.0, 1.0)


def survival_context_for_event(
    survival_table: pd.DataFrame,
    event_features: dict[str, Any],
) -> dict[str, Any]:
    if survival_table.empty:
        return {
            "method": "quantile_lightgbm",
            "censoring_rate": None,
            "sample_count": 0,
            "adjustment_factor": 1.0,
        }

    event_cause = normalize_event_cause(event_features.get("event_cause"))
    corridor = normalize_category(event_features.get("corridor"))
    hour_of_day, day_of_week = time_parts(event_features.get("start_datetime"))
    bucket = hour_bucket(hour_of_day)

    matchers = [
        (survival_table["corridor"] == corridor)
        & (survival_table["event_cause"] == event_cause)
        & (survival_table["hour_bucket"] == bucket)
        & (survival_table["day_of_week"] == day_of_week),
        (survival_table["corridor"] == corridor)
        & (survival_table["event_cause"] == event_cause),
        survival_table["corridor"] == corridor,
    ]
    for matcher in matchers:
        rows = survival_table.loc[matcher]
        if not rows.empty:
            row = rows.sort_values("sample_count", ascending=False).iloc[0]
            censoring_rate = float(row.get("censoring_rate") or 0.0)
            return {
                "method": "quantile_lightgbm_with_survival_censor_adjustment",
                "censoring_rate": clamp(censoring_rate, 0.0, 1.0),
                "sample_count": int(row.get("sample_count") or 0),
                "observed_median_duration": float(row.get("observed_median_duration") or 0.0),
                "adjustment_factor": clamp(1.0 + censoring_rate * 0.35, 1.0, 1.35),
            }

    censoring_rate = float(survival_table["censoring_rate"].mean())
    return {
        "method": "quantile_lightgbm_with_global_survival_censor_adjustment",
        "censoring_rate": clamp(censoring_rate, 0.0, 1.0),
        "sample_count": int(survival_table["sample_count"].sum()),
        "adjustment_factor": clamp(1.0 + censoring_rate * 0.25, 1.0, 1.25),
    }


def operational_metrics(
    event_features: dict[str, Any],
    severity_probability: float,
    severity_label: str,
    duration_low: float,
    duration_median: float,
    duration_high: float,
    risk_score: float,
) -> dict[str, Any]:
    context = operational_context_for_event(event_features)
    speed = context["speed"]
    sensors = context["sensors"]
    weather = context["weather"]

    delay_factor = float(speed.get("delay_factor") or 0.35)
    rain_factor = 1.0 + min(float(weather.get("rainfall_mm_1h") or 0.0) / 50.0, 0.35)
    closure_factor = 1.35 if severity_label == "HIGH" else 1.0
    expected_delay_minutes = duration_median * (0.35 + risk_score * 0.45 + delay_factor * 0.2) * rain_factor
    queue_length_m = (
        float(sensors.get("vehicle_count_15m") or 0)
        * (1.0 - float(speed.get("speed_ratio") or 0.65))
        * 4.8
    )
    affected_road_segments = max(1, round(1 + risk_score * 4 + (1 if severity_label == "HIGH" else 0)))
    clearance_time_minutes = duration_median * closure_factor + expected_delay_minutes * 0.2
    personnel_demand = max(4, round(4 + severity_probability * 8 + risk_score * 6))
    if is_minor_road_defect_context(event_features):
        personnel_demand = min(personnel_demand, 8)
    elif event_category_for_cause(event_features.get("event_cause")) in {"crash", "breakdown", "congestion"}:
        personnel_demand = min(personnel_demand, 12)

    explanation = [
        f"severity_probability={severity_probability:.2f} from event/corridor/station/time features",
        f"risk_score={risk_score:.2f} from historical corridor-hour density",
        f"speed_ratio={float(speed.get('speed_ratio') or 0.65):.2f} from fleet GPS adapter",
        f"rainfall_mm_1h={float(weather.get('rainfall_mm_1h') or 0.0):.1f} from weather adapter",
    ]
    if sensors.get("vehicle_count_15m"):
        explanation.append(f"vehicle_count_15m={int(sensors['vehicle_count_15m'])} from CCTV/ANPR adapter")
    if context["advisories"]:
        explanation.append(f"{len(context['advisories'])} active public advisory match")

    confidence_level = clamp(
        1.0
        - ((duration_high - duration_low) / max(duration_high, 1.0)) * 0.45
        - (0.12 if speed.get("sample_size", 0) == 0 else 0.0),
        0.35,
        0.92,
    )
    return {
        "expected_delay_minutes": max(1.0, expected_delay_minutes),
        "queue_length_m": max(0.0, queue_length_m),
        "affected_road_segments": affected_road_segments,
        "clearance_time_minutes": max(duration_median, clearance_time_minutes),
        "personnel_demand": personnel_demand,
        "confidence_level": confidence_level,
        "forecast_explanation": explanation,
        "human_override_allowed": True,
        "operational_context": context,
    }


def duration_cap_minutes(event_features: dict[str, Any]) -> float:
    return float(duration_cap_for_event(event_features, 8 * 60))


def predict_impact(event_features: dict[str, Any]) -> dict[str, Any]:
    artifacts = load_artifacts()
    severity_artifact = artifacts["severity"]
    severity_frame = build_feature_frame(event_features)
    severity_probability = float(
        severity_artifact["pipeline"].predict_proba(
            severity_frame[severity_artifact["features"]]
        )[0, 1]
    )
    threshold = float(severity_artifact.get("threshold", 0.5))
    severity_label = "HIGH" if severity_probability >= threshold else "LOW"

    closure_input = bool_to_float(event_features.get("requires_road_closure"))
    if closure_input is None:
        closure_input = 1.0 if severity_probability >= threshold else 0.0

    duration_frame = build_feature_frame(event_features, closure_input)
    duration_predictions = {
        name: float(artifact["pipeline"].predict(duration_frame[artifact["features"]])[0])
        for name, artifact in artifacts["duration"].items()
    }
    risk_score = risk_score_for_event(artifacts["risk_density"], event_features)
    survival = survival_context_for_event(artifacts["survival"], event_features)
    adjustment_factor = float(survival.get("adjustment_factor") or 1.0)
    cap_minutes = duration_cap_minutes(event_features)
    duration_low = clamp(
        max(1.0, duration_predictions["low"] * max(1.0, adjustment_factor * 0.92)),
        1.0,
        cap_minutes,
    )
    duration_median = clamp(
        max(duration_low, max(1.0, duration_predictions["median"] * adjustment_factor)),
        duration_low,
        cap_minutes,
    )
    duration_high = clamp(
        max(duration_median, max(1.0, duration_predictions["high"] * max(1.0, adjustment_factor * 1.08))),
        duration_median,
        cap_minutes,
    )
    metrics = operational_metrics(
        event_features,
        severity_probability,
        severity_label,
        duration_low,
        duration_median,
        duration_high,
        risk_score,
    )

    return {
        "severity_label": severity_label,
        "severity_probability": clamp(severity_probability, 0.0, 1.0),
        "duration_low": duration_low,
        "duration_median": duration_median,
        "duration_high": duration_high,
        "duration_confidence_interval": {
            "low": duration_low,
            "median": duration_median,
            "high": duration_high,
            "cap_minutes": cap_minutes,
            **survival,
        },
        "risk_score": risk_score,
        **metrics,
    }


if __name__ == "__main__":
    sample = {
        "event_cause": "Accident",
        "corridor": "Outer Ring Road",
        "zone": "East Zone 1",
        "police_station": "HAL Old Airport",
        "veh_type": "Car",
        "start_datetime": "2026-06-18T09:30:00+05:30",
    }
    print(json.dumps(predict_impact(sample), indent=2))
