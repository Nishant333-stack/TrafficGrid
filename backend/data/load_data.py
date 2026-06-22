from __future__ import annotations

import argparse
import hashlib
import os
import sys
from pathlib import Path
from typing import Any

import pandas as pd
from psycopg2.extras import execute_values
from sqlalchemy import create_engine, text

from backend.config.env_loader import load_project_env


load_project_env()


SOURCE_COLUMNS = [
    "id",
    "event_type",
    "latitude",
    "longitude",
    "address",
    "end_address",
    "event_cause",
    "requires_road_closure",
    "start_datetime",
    "end_datetime",
    "status",
    "description",
    "veh_type",
    "veh_no",
    "corridor",
    "priority",
    "route_path",
    "police_station",
    "closed_datetime",
    "resolved_datetime",
    "zone",
    "junction",
]

DATETIME_COLUMNS = [
    "start_datetime",
    "end_datetime",
    "closed_datetime",
    "resolved_datetime",
]

TEXT_COLUMNS = [
    column
    for column in SOURCE_COLUMNS
    if column
    not in {
        "latitude",
        "longitude",
        "requires_road_closure",
        *DATETIME_COLUMNS,
    }
]

EVENT_COLUMNS = SOURCE_COLUMNS + ["duration_minutes"]
NULL_SENTINELS = {"", "null", "none", "nan", "nat", "n/a", "na"}
BENGALURU_CENTER = (12.9716, 77.5946)
# Durations longer than this are treated as data-entry artifacts (a ticket left
# open for days/weeks) rather than real clearance times, and are nulled so they
# act as censored observations instead of skewing duration statistics/training.
# The dataset's median incident is ~64 min; the raw max is ~140 days.
MAX_PLAUSIBLE_DURATION_MINUTES = 24 * 60


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Load Astram event CSV data into the PostGIS-backed events table."
    )
    parser.add_argument("csv_path", type=Path, help="Path to the source CSV file")
    parser.add_argument(
        "--schema",
        type=Path,
        default=Path(__file__).with_name("schema.sql"),
        help="Path to schema.sql to apply before loading",
    )
    return parser.parse_args()


def require_database_url() -> str:
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        raise SystemExit("DATABASE_URL is required, e.g. postgresql+psycopg2://user:pass@localhost/db")
    return database_url


def apply_schema(engine: Any, schema_path: Path) -> None:
    if not schema_path.exists():
        raise SystemExit(f"Schema file not found: {schema_path}")

    with engine.begin() as connection:
        connection.exec_driver_sql(schema_path.read_text(encoding="utf-8"))


def normalize_null(value: Any) -> Any:
    if value is None:
        return pd.NA
    if isinstance(value, str) and value.strip().lower() in NULL_SENTINELS:
        return pd.NA
    return value


def normalize_text(series: pd.Series) -> pd.Series:
    return series.map(normalize_null).astype("string").str.strip().map(normalize_null)


def parse_bool(value: Any) -> Any:
    value = normalize_null(value)
    if pd.isna(value):
        return pd.NA

    normalized = str(value).strip().lower()
    if normalized in {"true", "t", "yes", "y", "1"}:
        return True
    if normalized in {"false", "f", "no", "n", "0"}:
        return False

    print(f"Warning: could not parse requires_road_closure value {value!r}; storing null")
    return pd.NA


def load_csv(csv_path: Path) -> tuple[pd.DataFrame, int, int, int]:
    if not csv_path.exists():
        raise SystemExit(f"CSV file not found: {csv_path}")

    raw = pd.read_csv(csv_path, dtype=str, keep_default_na=False, low_memory=False)
    missing = [column for column in SOURCE_COLUMNS if column not in raw.columns]
    if missing:
        raise SystemExit(f"CSV is missing required columns: {', '.join(missing)}")

    data = raw[SOURCE_COLUMNS].copy()
    for column in TEXT_COLUMNS:
        data[column] = normalize_text(data[column])

    missing_id_mask = data["id"].isna()
    missing_id_count = int(missing_id_mask.sum())
    if missing_id_count:
        data = data.loc[~missing_id_mask].copy()

    duplicate_id_count = int(data["id"].duplicated(keep="last").sum())
    if duplicate_id_count:
        data = data.drop_duplicates(subset=["id"], keep="last").copy()

    data["latitude"] = pd.to_numeric(data["latitude"].map(normalize_null), errors="coerce")
    data["longitude"] = pd.to_numeric(data["longitude"].map(normalize_null), errors="coerce")
    data["requires_road_closure"] = data["requires_road_closure"].map(parse_bool)

    for column in DATETIME_COLUMNS:
        data[column] = pd.to_datetime(
            data[column].map(normalize_null),
            errors="coerce",
            utc=True,
        )

    effective_end = data["closed_datetime"].combine_first(data["resolved_datetime"])
    duration = (effective_end - data["start_datetime"]).dt.total_seconds().div(60)
    has_duration_inputs = effective_end.notna() & data["start_datetime"].notna()
    # Null out durations that are negative (end before start) or implausibly long
    # (stale tickets). Both are data errors; nulling makes them censored.
    invalid_duration_mask = has_duration_inputs & (
        (duration < 0) | (duration > MAX_PLAUSIBLE_DURATION_MINUTES)
    )
    invalid_duration_count = int(invalid_duration_mask.sum())
    duration = duration.where(has_duration_inputs & ~invalid_duration_mask)
    data["duration_minutes"] = duration.round().astype("Int64")

    return data, missing_id_count, duplicate_id_count, invalid_duration_count


def db_value(value: Any) -> Any:
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    if isinstance(value, pd.Timestamp):
        return value.to_pydatetime()
    if hasattr(value, "item") and not isinstance(value, (str, bytes)):
        try:
            return value.item()
        except (AttributeError, ValueError):
            return value
    return value


def to_records(data: pd.DataFrame, columns: list[str]) -> list[tuple[Any, ...]]:
    return [
        tuple(db_value(value) for value in row)
        for row in data[columns].itertuples(index=False, name=None)
    ]


def upsert_events(engine: Any, data: pd.DataFrame) -> None:
    if data.empty:
        print("No event rows to insert.")
        return

    quoted_columns = ", ".join(f'"{column}"' for column in EVENT_COLUMNS)
    update_columns = ", ".join(
        f'"{column}" = EXCLUDED."{column}"'
        for column in EVENT_COLUMNS
        if column != "id"
    )
    sql = f"""
        INSERT INTO events ({quoted_columns})
        VALUES %s
        ON CONFLICT (id) DO UPDATE SET {update_columns}
    """

    raw_connection = engine.raw_connection()
    try:
        with raw_connection.cursor() as cursor:
            execute_values(cursor, sql, to_records(data, EVENT_COLUMNS), page_size=1000)
        raw_connection.commit()
    except Exception:
        raw_connection.rollback()
        raise
    finally:
        raw_connection.close()


def stable_int(seed: str, modulo: int) -> int:
    digest = hashlib.sha256(seed.encode("utf-8")).hexdigest()
    return int(digest[:8], 16) % modulo


def infer_zone(latitude: float, longitude: float) -> str:
    if latitude >= 13.03:
        return "North Zone 2"
    if longitude >= 77.66:
        return "East Zone 2"
    if longitude <= 77.55:
        return "West Zone 1"
    if latitude <= 12.94:
        return "South Zone 2"
    if 77.56 <= longitude <= 77.61:
        return "Central Zone 1"
    return "Central Zone 2"


def fallback_coordinate(name: str) -> tuple[float, float]:
    lat_jitter = (stable_int(f"{name}:lat", 1200) - 600) / 10000
    lon_jitter = (stable_int(f"{name}:lon", 1200) - 600) / 10000
    return BENGALURU_CENTER[0] + lat_jitter, BENGALURU_CENTER[1] + lon_jitter


def mode_or_none(series: pd.Series) -> Any:
    values = series.dropna()
    values = values[values.astype(str).str.strip() != ""]
    if values.empty:
        return None
    return values.mode().sort_values().iloc[0]


def build_station_seed(data: pd.DataFrame, limit: int = 15) -> list[tuple[Any, ...]]:
    station_values = data["police_station"].dropna()
    station_values = station_values[
        station_values.astype(str).str.strip().str.lower() != "no police station"
    ]
    names = station_values.value_counts().head(limit).index.tolist()
    rows: list[tuple[Any, ...]] = []

    for name in names:
        station_events = data.loc[data["police_station"] == name]
        latitude = station_events["latitude"].dropna().median()
        longitude = station_events["longitude"].dropna().median()
        if pd.isna(latitude) or pd.isna(longitude):
            latitude, longitude = fallback_coordinate(name)

        latitude = float(latitude)
        longitude = float(longitude)
        zone = mode_or_none(station_events["zone"]) or infer_zone(latitude, longitude)
        personnel = 12 + stable_int(f"{name}:personnel", 24)
        barricades = 18 + stable_int(f"{name}:barricades", 36)
        rows.append((name, zone, latitude, longitude, personnel, barricades))

    return rows


def reseed_police_stations(engine: Any, data: pd.DataFrame) -> int:
    rows = build_station_seed(data)

    if not rows:
        with engine.begin() as connection:
            connection.execute(text("TRUNCATE TABLE police_stations RESTART IDENTITY"))
        print("Warning: no police_station values found; police_stations was left empty.")
        return 0

    sql = """
        INSERT INTO police_stations
            (name, zone, latitude, longitude, available_personnel, available_barricades)
        VALUES %s
        ON CONFLICT (name) DO UPDATE SET
            zone = EXCLUDED.zone,
            latitude = EXCLUDED.latitude,
            longitude = EXCLUDED.longitude,
            available_personnel = EXCLUDED.available_personnel,
            available_barricades = EXCLUDED.available_barricades
    """

    raw_connection = engine.raw_connection()
    try:
        with raw_connection.cursor() as cursor:
            cursor.execute("TRUNCATE TABLE police_stations RESTART IDENTITY")
            execute_values(cursor, sql, rows, page_size=1000)
        raw_connection.commit()
    except Exception:
        raw_connection.rollback()
        raise
    finally:
        raw_connection.close()

    if len(rows) < 10:
        print(f"Warning: only seeded {len(rows)} police stations; CSV had fewer than 10 usable names.")
    return len(rows)


def print_sanity(engine: Any) -> None:
    stats_sql = text(
        """
        SELECT
            COUNT(*) AS event_count,
            MIN(start_datetime) AS min_start,
            MAX(start_datetime) AS max_start,
            COUNT(*) FILTER (WHERE geom IS NOT NULL) AS events_with_geom,
            COUNT(*) FILTER (WHERE duration_minutes IS NULL) AS duration_nulls
        FROM events
        """
    )
    nulls_sql = text(
        """
        SELECT
            COUNT(*) FILTER (WHERE latitude IS NULL) AS latitude_nulls,
            COUNT(*) FILTER (WHERE longitude IS NULL) AS longitude_nulls,
            COUNT(*) FILTER (WHERE start_datetime IS NULL) AS start_datetime_nulls,
            COUNT(*) FILTER (WHERE zone IS NULL OR btrim(zone) = '') AS zone_nulls,
            COUNT(*) FILTER (WHERE police_station IS NULL OR btrim(police_station) = '') AS police_station_nulls,
            COUNT(*) FILTER (WHERE geom IS NULL) AS geom_nulls
        FROM events
        """
    )

    with engine.connect() as connection:
        stats = connection.execute(stats_sql).mappings().one()
        nulls = connection.execute(nulls_sql).mappings().one()
        station_count = connection.execute(text("SELECT COUNT(*) FROM police_stations")).scalar_one()

    print("\nSanity check")
    print(f"Events in table: {stats['event_count']}")
    print(f"Start date range: {stats['min_start']} -> {stats['max_start']}")
    print(f"Events with geometry: {stats['events_with_geom']}")
    print(f"Police stations seeded: {station_count}")
    print(f"Null duration_minutes: {stats['duration_nulls']}")
    print(
        "Null key columns: "
        f"latitude={nulls['latitude_nulls']}, "
        f"longitude={nulls['longitude_nulls']}, "
        f"start_datetime={nulls['start_datetime_nulls']}, "
        f"zone={nulls['zone_nulls']}, "
        f"police_station={nulls['police_station_nulls']}, "
        f"geom={nulls['geom_nulls']}"
    )


def main() -> None:
    args = parse_args()
    engine = create_engine(require_database_url(), future=True)

    print(f"Applying schema from {args.schema}")
    apply_schema(engine, args.schema)

    data, missing_id_count, duplicate_id_count, negative_duration_count = load_csv(args.csv_path)
    if missing_id_count:
        print(f"Warning: dropped {missing_id_count} rows with missing id")
    if duplicate_id_count:
        print(f"Warning: found {duplicate_id_count} duplicate ids; kept the last row for each id")
    if negative_duration_count:
        print(f"Warning: set {negative_duration_count} negative durations to null")

    print(f"Loaded {len(data)} event rows from {args.csv_path}")
    upsert_events(engine, data)
    station_count = reseed_police_stations(engine, data)
    print(f"Upserted {len(data)} events and seeded {station_count} police stations.")
    print_sanity(engine)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit("Interrupted")
