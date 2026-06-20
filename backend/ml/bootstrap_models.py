"""Create minimal ML artifacts when trained models are not bundled."""

from __future__ import annotations

import os
from pathlib import Path

import joblib
import numpy as np
import pandas as pd
from sklearn.compose import ColumnTransformer
from sklearn.linear_model import LogisticRegression, QuantileRegressor
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import OneHotEncoder

from backend.config.paths import PROJECT_ROOT
from backend.ml.feature_cleaning import (
    event_category_for_cause,
    normalize_category,
    normalize_event_cause,
)


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
RANDOM_STATE = 42
HOUR_BUCKET_SIZE = 3


def hour_bucket(hour_of_day: int) -> int:
    if hour_of_day < 0:
        return -1
    return int(hour_of_day // HOUR_BUCKET_SIZE * HOUR_BUCKET_SIZE)


def make_preprocessor(feature_columns: list[str]) -> ColumnTransformer:
    categorical = [column for column in CATEGORICAL_FEATURES if column in feature_columns]
    numeric = [column for column in feature_columns if column not in categorical]
    return ColumnTransformer(
        transformers=[
            ("categorical", OneHotEncoder(handle_unknown="ignore", sparse_output=False), categorical),
            ("numeric", "passthrough", numeric),
        ],
        remainder="drop",
    )


def synthetic_training_frame(rows: int = 240) -> pd.DataFrame:
    rng = np.random.default_rng(RANDOM_STATE)
    causes = ["accident", "breakdown", "congestion", "pothole"]
    corridors = ["M G Road", "Outer Ring Road", "Hosur Road", "Bellary Road"]
    zones = ["Central Zone 1", "East Zone 1", "North Zone 1", "West Zone 1"]
    stations = ["Cubbon Park", "High ground", "Halasuru Gate", "Yelahanka"]
    records: list[dict[str, object]] = []

    for index in range(rows):
        cause = causes[index % len(causes)]
        corridor = corridors[index % len(corridors)]
        hour = int(rng.integers(0, 24))
        day = int(rng.integers(0, 7))
        closure = float(index % 3 == 0)
        base_duration = 18 + (hour % 6) * 4 + (10 if cause == "accident" else 4)
        records.append(
            {
                "event_type": "incident",
                "event_category": event_category_for_cause(cause),
                "event_cause": normalize_event_cause(cause),
                "corridor": normalize_category(corridor),
                "zone": normalize_category(zones[index % len(zones)]),
                "police_station": normalize_category(stations[index % len(stations)]),
                "priority": "high" if closure else "low",
                "veh_type": "car",
                "hour_of_day": hour,
                "day_of_week": day,
                "requires_road_closure": closure,
                "duration_minutes": float(base_duration + rng.integers(0, 20)),
            }
        )

    return pd.DataFrame.from_records(records)


def write_artifacts(model_dir: Path, data: pd.DataFrame) -> None:
    model_dir.mkdir(parents=True, exist_ok=True)

    severity_pipeline = Pipeline(
        steps=[
            ("preprocess", make_preprocessor(SEVERITY_FEATURES)),
            (
                "model",
                LogisticRegression(max_iter=500, random_state=RANDOM_STATE),
            ),
        ]
    )
    severity_pipeline.fit(data[SEVERITY_FEATURES], data["requires_road_closure"].astype(int))
    joblib.dump(
        {
            "pipeline": severity_pipeline,
            "features": SEVERITY_FEATURES,
            "threshold": 0.5,
            "labels": {0: "LOW", 1: "HIGH"},
        },
        model_dir / "severity_model.pkl",
    )

    duration_frame = data[DURATION_FEATURES].copy()
    duration_frame["requires_road_closure"] = duration_frame["requires_road_closure"].fillna(0.0)
    y = data["duration_minutes"]
    for quantile, suffix in ((0.25, "q25"), (0.5, "q50"), (0.75, "q75")):
        pipeline = Pipeline(
            steps=[
                ("preprocess", make_preprocessor(DURATION_FEATURES)),
                (
                    "model",
                    QuantileRegressor(quantile=quantile, alpha=0.0, solver="highs"),
                ),
            ]
        )
        pipeline.fit(duration_frame, y)
        joblib.dump(
            {
                "pipeline": pipeline,
                "features": DURATION_FEATURES,
                "quantile": quantile,
            },
            model_dir / f"duration_{suffix}_model.pkl",
        )

    risk = data.copy()
    risk["hour_bucket"] = risk["hour_of_day"].map(hour_bucket)
    risk = (
        risk.groupby(["corridor", "hour_bucket", "day_of_week"], dropna=False)
        .size()
        .reset_index(name="event_count")
    )
    max_per_corridor = risk.groupby("corridor")["event_count"].transform("max")
    risk["risk_score"] = (risk["event_count"] / max_per_corridor).clip(0, 1)
    risk.to_parquet(model_dir / "risk_density.parquet", index=False)

    survival = data.copy()
    survival["hour_bucket"] = survival["hour_of_day"].map(hour_bucket)
    survival["row_id"] = range(len(survival))
    survival = (
        survival.groupby(["corridor", "event_cause", "hour_bucket", "day_of_week"], dropna=False)
        .agg(
            sample_count=("row_id", "count"),
            observed_median_duration=("duration_minutes", "median"),
        )
        .reset_index()
    )
    survival["censoring_rate"] = 0.08
    survival.to_parquet(model_dir / "duration_survival_table.parquet", index=False)


def bootstrap_models(model_dir: Path | None = None) -> Path:
    target = Path(model_dir or MODEL_DIR)
    required = [
        target / "severity_model.pkl",
        target / "duration_q25_model.pkl",
        target / "duration_q50_model.pkl",
        target / "duration_q75_model.pkl",
        target / "risk_density.parquet",
    ]
    if all(path.exists() for path in required):
        return target

    data = synthetic_training_frame()
    write_artifacts(target, data)
    return target


if __name__ == "__main__":
    path = bootstrap_models()
    print(f"Model artifacts ready in {path} (project root: {PROJECT_ROOT})")
