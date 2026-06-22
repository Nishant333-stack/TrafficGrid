from __future__ import annotations

import argparse
import os
import subprocess
import sys
import warnings
from pathlib import Path
from typing import Any

from backend.config.env_loader import load_project_env


load_project_env()

os.environ.setdefault("LOKY_MAX_CPU_COUNT", "4")
os.environ.setdefault("MPLCONFIGDIR", "/private/tmp/grid_matplotlib")
Path(os.environ["MPLCONFIGDIR"]).mkdir(parents=True, exist_ok=True)
warnings.filterwarnings(
    "ignore",
    message="X does not have valid feature names.*",
    category=UserWarning,
)

import joblib
import numpy as np
import pandas as pd
from sklearn.compose import ColumnTransformer
from sklearn.metrics import accuracy_score, mean_absolute_error, roc_auc_score
from sklearn.model_selection import train_test_split
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import OneHotEncoder
from sqlalchemy import create_engine, text

from backend.ml.feature_cleaning import (
    duration_cap_for_event,
    event_category_for_cause,
    normalize_category,
    normalize_event_cause,
)


RANDOM_STATE = 42
MODEL_DIR = Path(__file__).with_name("models")

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
TIME_FEATURES = ["hour_of_day", "day_of_week"]
SEVERITY_FEATURES = CATEGORICAL_FEATURES + TIME_FEATURES
DURATION_FEATURES = SEVERITY_FEATURES + ["requires_road_closure"]
HOUR_BUCKET_SIZE = 3

EVENT_QUERY = """
SELECT
    id,
    event_type,
    event_cause,
    corridor,
    zone,
    police_station,
    priority,
    veh_type,
    start_datetime,
    requires_road_closure,
    duration_minutes
FROM events
"""
MAX_TRAINING_DURATION_MINUTES = int(os.environ.get("MAX_TRAINING_DURATION_MINUTES", str(24 * 60)))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Train severity, duration, and risk-density artifacts from the events table."
    )
    parser.add_argument(
        "--csv",
        type=Path,
        help="Optional local CSV fallback for development when DATABASE_URL is not available",
    )
    parser.add_argument(
        "--models-dir",
        type=Path,
        default=MODEL_DIR,
        help="Directory where trained model artifacts will be written",
    )
    parser.add_argument(
        "--preprocessed-path",
        type=Path,
        help="Optional path for the cached preprocessed training frame",
    )
    parser.add_argument(
        "--stage",
        choices=["all", "duration", "severity", "risk"],
        default="all",
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--no-feedback",
        action="store_true",
        help="Skip merging officer-reported actual durations into the training labels",
    )
    return parser.parse_args()


def database_url() -> str:
    value = os.environ.get("DATABASE_URL")
    if not value:
        raise SystemExit(
            "DATABASE_URL is required unless --csv is provided, "
            "e.g. postgresql+psycopg2://user:pass@localhost/db"
        )
    return value


def bool_to_float(value: Any) -> float:
    if value is None:
        return np.nan
    try:
        if pd.isna(value):
            return np.nan
    except (TypeError, ValueError):
        pass

    if isinstance(value, bool):
        return 1.0 if value else 0.0

    normalized = str(value).strip().lower()
    if normalized in {"true", "t", "yes", "y", "1"}:
        return 1.0
    if normalized in {"false", "f", "no", "n", "0"}:
        return 0.0
    return np.nan


def clean_duration_minutes(row: pd.Series) -> float:
    raw_duration = row.get("raw_duration_minutes")
    if pd.isna(raw_duration) or raw_duration < 0:
        return np.nan

    cap_minutes = duration_cap_for_event(row.to_dict(), MAX_TRAINING_DURATION_MINUTES)
    if raw_duration > cap_minutes:
        return np.nan
    return float(raw_duration)


def load_events_from_db() -> pd.DataFrame:
    engine = create_engine(database_url(), future=True)
    return pd.read_sql_query(text(EVENT_QUERY), engine)


def load_events_from_csv(csv_path: Path) -> pd.DataFrame:
    from backend.data.load_data import load_csv

    data, missing_ids, duplicate_ids, negative_durations = load_csv(csv_path)
    if missing_ids:
        print(f"Warning: CSV loader dropped {missing_ids} rows with missing id")
    if duplicate_ids:
        print(f"Warning: CSV loader found {duplicate_ids} duplicate ids and kept the last")
    if negative_durations:
        print(f"Warning: CSV loader set {negative_durations} negative durations to null")
    return data[
        [
            "id",
            "event_type",
            "event_cause",
            "corridor",
            "zone",
            "police_station",
            "priority",
            "veh_type",
            "start_datetime",
            "requires_road_closure",
            "duration_minutes",
        ]
    ].copy()


def apply_feedback_labels(events: pd.DataFrame) -> tuple[pd.DataFrame, int]:
    """Override duration labels with officer-reported actual durations.

    This is the post-event learning step: when a field officer reports the real
    clearance time via feedback, that ground truth supersedes the timestamp-
    derived duration so the next model trains on corrected outcomes. The most
    recent feedback per event wins.
    """
    if "id" not in events.columns:
        return events, 0
    try:
        from backend.data.workflow import feedback_rows

        rows = feedback_rows()
    except Exception as exc:  # pragma: no cover - feedback store optional
        print(f"Warning: could not read feedback for label merge: {exc}", flush=True)
        return events, 0

    actuals: dict[str, float] = {}
    for row in rows:
        event_id = row.get("event_id")
        actual = row.get("actual_duration_minutes")
        if event_id is None or actual in (None, ""):
            continue
        try:
            value = float(actual)
        except (TypeError, ValueError):
            continue
        if value > 0:
            actuals[str(event_id)] = value

    if not actuals:
        return events, 0

    frame = events.copy()
    ids = frame["id"].astype(str)
    mask = ids.isin(actuals)
    if not mask.any():
        return frame, 0
    frame.loc[mask, "duration_minutes"] = ids[mask].map(actuals).astype(float)
    return frame, int(mask.sum())


def add_derived_features(data: pd.DataFrame) -> pd.DataFrame:
    frame = data.copy()
    start = pd.to_datetime(frame.get("start_datetime"), errors="coerce", utc=True)
    frame["hour_of_day"] = start.dt.hour.fillna(-1).astype(int)
    frame["day_of_week"] = start.dt.dayofweek.fillna(-1).astype(int)

    for column in CATEGORICAL_FEATURES:
        if column not in frame:
            frame[column] = "UNKNOWN"
        if column == "event_cause":
            frame[column] = frame[column].map(normalize_event_cause)
        elif column == "event_category":
            frame[column] = frame.get("event_cause", "UNKNOWN").map(event_category_for_cause)
        else:
            frame[column] = frame[column].map(normalize_category)

    frame["event_category"] = frame["event_cause"].map(event_category_for_cause)

    if "requires_road_closure" in frame:
        frame["requires_road_closure"] = frame["requires_road_closure"].map(bool_to_float)
    else:
        frame["requires_road_closure"] = np.nan

    frame["raw_duration_minutes"] = pd.to_numeric(
        frame.get("duration_minutes"), errors="coerce"
    )
    frame["duration_minutes"] = frame.apply(clean_duration_minutes, axis=1)
    frame["duration_was_outlier"] = (
        frame["raw_duration_minutes"].notna()
        & frame["duration_minutes"].isna()
        & (frame["raw_duration_minutes"] >= 0)
    )
    return frame


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


def train_severity(data: pd.DataFrame, model_dir: Path) -> float:
    from xgboost import XGBClassifier

    model_dir.mkdir(parents=True, exist_ok=True)
    trainable = data.loc[data["requires_road_closure"].notna()].copy()
    if trainable.empty:
        raise SystemExit("No rows with requires_road_closure target are available.")

    y = trainable["requires_road_closure"].astype(int)
    if y.nunique() < 2:
        raise SystemExit("Severity model needs both closure and non-closure examples.")

    x = trainable[SEVERITY_FEATURES]
    stratify = y if y.value_counts().min() >= 2 else None
    x_train, x_test, y_train, y_test = train_test_split(
        x,
        y,
        test_size=0.2,
        random_state=RANDOM_STATE,
        stratify=stratify,
    )

    positive = int(y_train.sum())
    negative = int(len(y_train) - positive)
    scale_pos_weight = negative / positive if positive else 1.0

    model = XGBClassifier(
        objective="binary:logistic",
        eval_metric="logloss",
        n_estimators=90,
        max_depth=3,
        learning_rate=0.08,
        subsample=0.9,
        colsample_bytree=0.9,
        reg_lambda=1.0,
        scale_pos_weight=scale_pos_weight,
        random_state=RANDOM_STATE,
        n_jobs=2,
    )
    pipeline = Pipeline(
        steps=[
            ("preprocess", make_preprocessor(SEVERITY_FEATURES)),
            ("model", model),
        ]
    )
    pipeline.fit(x_train, y_train)

    probabilities = pipeline.predict_proba(x_test)[:, 1]
    predictions = (probabilities >= 0.5).astype(int)
    accuracy = accuracy_score(y_test, predictions)
    auc = roc_auc_score(y_test, probabilities) if y_test.nunique() == 2 else np.nan

    joblib.dump(
        {
            "pipeline": pipeline,
            "features": SEVERITY_FEATURES,
            "threshold": 0.5,
            "labels": {0: "LOW", 1: "HIGH"},
        },
        model_dir / "severity_model.pkl",
    )

    positive_rate = float(y.mean())
    print(
        "Model A severity classifier: "
        f"rows={len(trainable)}, positive_rate={positive_rate:.3f}, "
        f"holdout_accuracy={accuracy:.3f}, holdout_auc={auc:.3f}"
    )
    return float(accuracy)


def train_duration(data: pd.DataFrame, model_dir: Path) -> float:
    from lightgbm import LGBMRegressor

    model_dir.mkdir(parents=True, exist_ok=True)
    trainable = data.loc[
        data["duration_minutes"].notna() & (data["duration_minutes"] >= 0)
    ].copy()
    outlier_count = int((trainable["duration_minutes"] > MAX_TRAINING_DURATION_MINUTES).sum())
    if outlier_count:
        trainable = trainable.loc[trainable["duration_minutes"] <= MAX_TRAINING_DURATION_MINUTES].copy()
        print(
            "Duration outlier filter: "
            f"excluded {outlier_count} rows above {MAX_TRAINING_DURATION_MINUTES} minutes",
            flush=True,
        )
    if len(trainable) < 50:
        raise SystemExit(
            f"Duration model needs at least 50 valid duration rows; found {len(trainable)}."
        )

    trainable["requires_road_closure"] = trainable["requires_road_closure"].fillna(0.0)
    x = trainable[DURATION_FEATURES]
    y = trainable["duration_minutes"].astype(float)

    x_train, x_test, y_train, y_test = train_test_split(
        x,
        y,
        test_size=0.2,
        random_state=RANDOM_STATE,
    )

    median_predictions = None
    for quantile in (0.25, 0.5, 0.75):
        print(f"Training duration quantile model q={quantile:.2f}...", flush=True)
        model = LGBMRegressor(
            objective="quantile",
            alpha=quantile,
            n_estimators=70,
            learning_rate=0.08,
            num_leaves=16,
            min_child_samples=25,
            subsample=0.9,
            colsample_bytree=0.9,
            random_state=RANDOM_STATE,
            force_col_wise=True,
            n_jobs=1,
            verbosity=-1,
        )
        pipeline = Pipeline(
            steps=[
                ("preprocess", make_preprocessor(DURATION_FEATURES)),
                ("model", model),
            ]
        )
        pipeline.fit(x_train, y_train)
        artifact = {
            "pipeline": pipeline,
            "features": DURATION_FEATURES,
            "quantile": quantile,
            "max_training_duration_minutes": MAX_TRAINING_DURATION_MINUTES,
            "excluded_outlier_count": outlier_count,
        }
        suffix = int(quantile * 100)
        joblib.dump(artifact, model_dir / f"duration_q{suffix}_model.pkl")

        if quantile == 0.5:
            median_predictions = pipeline.predict(x_test)

    if median_predictions is None:
        raise RuntimeError("Median duration model was not trained.")

    mae = mean_absolute_error(y_test, median_predictions)
    median_absolute_error = float(np.median(np.abs(np.asarray(y_test) - median_predictions)))
    coverage = len(trainable) / max(len(data), 1)
    print(
        "Model B duration quantile regressors: "
        f"rows={len(trainable)}, coverage={coverage:.3f}, "
        f"holdout_median_mae={mae:.1f} minutes, "
        f"holdout_median_absolute_error={median_absolute_error:.1f} minutes"
    )
    return float(mae)


def hour_bucket(hour_of_day: int) -> int:
    if hour_of_day < 0:
        return -1
    return int(hour_of_day // HOUR_BUCKET_SIZE * HOUR_BUCKET_SIZE)


def build_risk_density(data: pd.DataFrame, model_dir: Path) -> pd.DataFrame:
    risk_data = data.loc[data["hour_of_day"] >= 0].copy()
    risk_data["hour_bucket"] = risk_data["hour_of_day"].map(hour_bucket)

    grouped = (
        risk_data.groupby(["corridor", "hour_bucket", "day_of_week"], dropna=False)
        .size()
        .reset_index(name="event_count")
    )
    if grouped.empty:
        grouped["risk_score"] = []
    else:
        max_per_corridor = grouped.groupby("corridor")["event_count"].transform("max")
        grouped["risk_score"] = (grouped["event_count"] / max_per_corridor).clip(0, 1)

    grouped = grouped.sort_values(
        ["corridor", "day_of_week", "hour_bucket"], kind="stable"
    ).reset_index(drop=True)
    grouped.to_parquet(model_dir / "risk_density.parquet", index=False)
    print(
        "Model C risk-density lookup: "
        f"rows={len(grouped)}, corridors={grouped['corridor'].nunique() if not grouped.empty else 0}"
    )
    return grouped


def build_survival_duration_table(data: pd.DataFrame, model_dir: Path) -> pd.DataFrame:
    survival_data = data.copy()
    survival_data["hour_bucket"] = survival_data["hour_of_day"].map(hour_bucket)
    survival_data["duration_observed"] = (
        survival_data["duration_minutes"].notna()
        & (survival_data["duration_minutes"] >= 0)
    )

    grouped = (
        survival_data.groupby(
            ["corridor", "event_cause", "hour_bucket", "day_of_week"],
            dropna=False,
        )
        .agg(
            sample_count=("id", "count"),
            observed_count=("duration_observed", "sum"),
            observed_median_duration=("duration_minutes", "median"),
            observed_p75_duration=("duration_minutes", lambda values: values.dropna().quantile(0.75)),
        )
        .reset_index()
    )
    if grouped.empty:
        grouped["censoring_rate"] = []
    else:
        grouped["censoring_rate"] = (
            1.0 - grouped["observed_count"] / grouped["sample_count"].clip(lower=1)
        ).clip(0, 1)
        grouped["observed_median_duration"] = grouped["observed_median_duration"].fillna(
            float(survival_data["duration_minutes"].median(skipna=True) or 60.0)
        )
        grouped["observed_p75_duration"] = grouped["observed_p75_duration"].fillna(
            grouped["observed_median_duration"]
        )

    grouped = grouped.sort_values(
        ["corridor", "event_cause", "day_of_week", "hour_bucket"],
        kind="stable",
    ).reset_index(drop=True)
    grouped.to_parquet(model_dir / "duration_survival_table.parquet", index=False)
    print(
        "Survival duration table: "
        f"rows={len(grouped)}, censoring_rate={grouped['censoring_rate'].mean() if not grouped.empty else 0:.3f}"
    )
    return grouped


def print_data_summary(data: pd.DataFrame) -> None:
    duration_valid = int(
        (data["duration_minutes"].notna() & (data["duration_minutes"] >= 0)).sum()
    )
    closure_valid = int(data["requires_road_closure"].notna().sum())
    print(
        "Training data: "
        f"events={len(data)}, closure_targets={closure_valid}, "
        f"valid_durations={duration_valid}, "
        f"duration_outliers_cleaned={int(data.get('duration_was_outlier', pd.Series(dtype=bool)).sum())}, "
        f"corridors={data['corridor'].nunique()}"
    )


def preprocess_and_cache(
    events: pd.DataFrame,
    preprocessed_path: Path,
) -> pd.DataFrame:
    preprocessed_path.parent.mkdir(parents=True, exist_ok=True)
    data = add_derived_features(events)
    data.to_parquet(preprocessed_path, index=False)
    print(f"Preprocessed training frame saved to {preprocessed_path.resolve()}", flush=True)
    return pd.read_parquet(preprocessed_path)


def run_stage(stage: str, models_dir: Path, preprocessed_path: Path) -> None:
    command = [
        sys.executable,
        "-u",
        str(Path(__file__).resolve()),
        "--stage",
        stage,
        "--models-dir",
        str(models_dir),
        "--preprocessed-path",
        str(preprocessed_path),
    ]
    print(f"Running training stage: {stage}", flush=True)
    subprocess.run(command, check=True)


def run_single_stage(stage: str, models_dir: Path, preprocessed_path: Path) -> None:
    if not preprocessed_path.exists():
        raise SystemExit(
            f"Preprocessed training frame not found: {preprocessed_path}. "
            "Run python train_models.py first."
        )

    data = pd.read_parquet(preprocessed_path)
    if data.empty:
        raise SystemExit("No events found in preprocessed training frame.")

    if stage == "duration":
        train_duration(data, models_dir)
    elif stage == "severity":
        train_severity(data, models_dir)
    elif stage == "risk":
        build_risk_density(data, models_dir)
        build_survival_duration_table(data, models_dir)
    else:
        raise SystemExit(f"Unknown training stage: {stage}")


def run_training(
    models_dir: Path = MODEL_DIR,
    csv: Path | None = None,
    preprocessed_path: Path | None = None,
    use_feedback: bool = True,
) -> dict[str, Any]:
    """Train all artifacts from events (+ officer feedback) into ``models_dir``.

    Returns a summary so callers (CLI, retrain endpoint) can report what happened.
    Importable so the running API can retrain and reload without a subprocess.
    """
    models_dir = Path(models_dir)
    models_dir.mkdir(parents=True, exist_ok=True)
    preprocessed_path = preprocessed_path or models_dir / "training_events_preprocessed.parquet"

    if csv:
        events = load_events_from_csv(csv)
        print(f"Loaded training data from CSV: {csv}", flush=True)
    else:
        events = load_events_from_db()
        print("Loaded training data from Postgres events table", flush=True)

    feedback_labels = 0
    if use_feedback:
        events, feedback_labels = apply_feedback_labels(events)
        print(f"Applied {feedback_labels} officer-reported duration labels", flush=True)

    data = preprocess_and_cache(events, preprocessed_path)
    if data.empty:
        raise SystemExit("No events found for training.")

    print_data_summary(data)
    for stage in ("duration", "severity", "risk"):
        run_stage(stage, models_dir, preprocessed_path)
    print(f"Saved model artifacts to {models_dir.resolve()}")

    return {
        "events_used": int(len(data)),
        "feedback_labels_applied": feedback_labels,
        "models_dir": str(models_dir.resolve()),
    }


def main() -> None:
    args = parse_args()
    args.models_dir.mkdir(parents=True, exist_ok=True)
    preprocessed_path = (
        args.preprocessed_path
        if args.preprocessed_path
        else args.models_dir / "training_events_preprocessed.parquet"
    )

    if args.stage != "all":
        run_single_stage(args.stage, args.models_dir, preprocessed_path)
        return

    run_training(
        models_dir=args.models_dir,
        csv=args.csv,
        preprocessed_path=preprocessed_path,
        use_feedback=not args.no_feedback,
    )


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit("Interrupted")
