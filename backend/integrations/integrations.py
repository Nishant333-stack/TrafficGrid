from __future__ import annotations

import json
import os
import ssl
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo


APP_ROOT = Path(__file__).resolve().parent
INTEGRATION_DIR = APP_ROOT / "integrations"
LOCAL_TZ = ZoneInfo("Asia/Kolkata")

# Live weather (Open-Meteo: free, no API key). Bengaluru by default; overridable.
WEATHER_LAT = float(os.environ.get("WEATHER_LAT", "12.9716"))
WEATHER_LON = float(os.environ.get("WEATHER_LON", "77.5946"))
WEATHER_TTL_SECONDS = float(os.environ.get("WEATHER_TTL_SECONDS", "600"))
WEATHER_TIMEOUT_SECONDS = float(os.environ.get("WEATHER_TIMEOUT_SECONDS", "4"))
# Per-coordinate cache so each event location gets its own live reading.
_WEATHER_CACHE: dict[str, dict[str, Any]] = {}
_LAST_WEATHER_MODE: str = "fixture"
_LAST_WEATHER_AT: float = 0.0


def _ssl_context() -> ssl.SSLContext | None:
    """Verified TLS context using certifi's CA bundle.

    Works even on slim images that lack a system CA store. Falls back to the
    default context if certifi is unavailable.
    """
    try:
        import certifi

        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return None


@dataclass(frozen=True)
class FeedStatus:
    name: str
    category: str
    mode: str
    records: int
    last_seen: str
    freshness_seconds: int
    health: str = "ok"

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "category": self.category,
            "mode": self.mode,
            "records": self.records,
            "last_seen": self.last_seen,
            "freshness_seconds": self.freshness_seconds,
            "health": self.health,
        }


def now_ist() -> datetime:
    return datetime.now(LOCAL_TZ)


def iso_at(minutes_delta: int) -> str:
    return (now_ist() + timedelta(minutes=minutes_delta)).isoformat()


def read_json_feed(file_name: str) -> list[dict[str, Any]] | None:
    path = INTEGRATION_DIR / file_name
    if not path.exists():
        return None
    payload = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict) and isinstance(payload.get("records"), list):
        return payload["records"]
    return None


def live_incidents() -> list[dict[str, Any]]:
    configured = read_json_feed("live_incidents.json")
    if configured is not None:
        return configured

    return [
        {
            "id": "astra-live-silk-board-01",
            "source": "astra_live_incident_feed",
            "event_type": "incident",
            "name": "Crash near Silk Board flyover",
            "latitude": 12.91786,
            "longitude": 77.62391,
            "event_cause": "accident",
            "priority": "High",
            "status": "active",
            "corridor": "Outer Ring Road",
            "zone": "South East Zone",
            "police_station": "Madiwala",
            "start_datetime": iso_at(-18),
            "requires_road_closure": True,
            "feed_confidence": 0.92,
        },
        {
            "id": "astra-live-mg-road-02",
            "source": "astra_live_incident_feed",
            "event_type": "incident",
            "name": "Waterlogging near Trinity circle",
            "latitude": 12.97382,
            "longitude": 77.61718,
            "event_cause": "waterlogging",
            "priority": "High",
            "status": "active",
            "corridor": "M G Road",
            "zone": "Central Zone 1",
            "police_station": "Cubbon Park",
            "start_datetime": iso_at(-34),
            "requires_road_closure": False,
            "feed_confidence": 0.87,
        },
        {
            "id": "astra-live-hebbal-03",
            "source": "astra_live_incident_feed",
            "event_type": "incident",
            "name": "Slow traffic at Hebbal loop",
            "latitude": 13.03568,
            "longitude": 77.58963,
            "event_cause": "breakdown",
            "priority": "Low",
            "status": "active",
            "corridor": "Bellary Road",
            "zone": "North Zone 1",
            "police_station": "Hebbal",
            "start_datetime": iso_at(-11),
            "requires_road_closure": False,
            "feed_confidence": 0.78,
        },
    ]


def planned_permits() -> list[dict[str, Any]]:
    configured = read_json_feed("planned_permits.json")
    if configured is not None:
        return configured

    return [
        {
            "id": "permit-kanteerava-sports-01",
            "source": "planned_permit_feed",
            "event_type": "planned",
            "name": "Evening football crowd, Kanteerava",
            "latitude": 12.96978,
            "longitude": 77.59373,
            "event_cause": "public_event",
            "priority": "High",
            "status": "planned",
            "corridor": "Kasturba Road",
            "zone": "Central Zone 1",
            "police_station": "Cubbon Park",
            "scheduled_start": iso_at(210),
            "requires_road_closure": False,
        },
        {
            "id": "permit-whitefield-metro-work-02",
            "source": "planned_permit_feed",
            "event_type": "planned",
            "name": "Night utility work, Whitefield corridor",
            "latitude": 12.96995,
            "longitude": 77.74997,
            "event_cause": "construction",
            "priority": "Low",
            "status": "planned",
            "corridor": "Whitefield Main Road",
            "zone": "East Zone 2",
            "police_station": "K.R. Pura",
            "scheduled_start": iso_at(390),
            "requires_road_closure": True,
        },
    ]


def gps_speed_feed() -> list[dict[str, Any]]:
    configured = read_json_feed("gps_speeds.json")
    if configured is not None:
        return configured

    return [
        {"corridor": "Outer Ring Road", "current_speed_kmph": 13.8, "free_flow_speed_kmph": 38.0, "sample_size": 428},
        {"corridor": "M G Road", "current_speed_kmph": 11.9, "free_flow_speed_kmph": 31.0, "sample_size": 214},
        {"corridor": "Bellary Road", "current_speed_kmph": 24.2, "free_flow_speed_kmph": 44.0, "sample_size": 173},
        {"corridor": "Kasturba Road", "current_speed_kmph": 16.4, "free_flow_speed_kmph": 29.0, "sample_size": 96},
        {"corridor": "Whitefield Main Road", "current_speed_kmph": 18.5, "free_flow_speed_kmph": 36.0, "sample_size": 188},
    ]


def _rain_intensity(rainfall_mm_1h: float) -> str:
    if rainfall_mm_1h <= 0.1:
        return "none"
    if rainfall_mm_1h < 2.5:
        return "light"
    if rainfall_mm_1h < 7.6:
        return "moderate"
    return "heavy"


def _live_weather_enabled() -> bool:
    return os.environ.get("LIVE_WEATHER", "true").strip().lower() not in {"false", "0", "no", "off"}


def _weather_conditions(code: Any) -> str:
    try:
        code = int(code)
    except (TypeError, ValueError):
        return "unknown"
    if code == 0:
        return "clear"
    if code in (1, 2, 3):
        return "cloudy"
    if code in (45, 48):
        return "fog"
    if 51 <= code <= 57:
        return "drizzle"
    if 61 <= code <= 67:
        return "rain"
    if 71 <= code <= 77:
        return "snow"
    if 80 <= code <= 82:
        return "rain_showers"
    if 95 <= code <= 99:
        return "thunderstorm"
    return "unknown"


def _fetch_live_weather(lat: float, lon: float) -> dict[str, Any] | None:
    """Fetch current weather for a point from Open-Meteo (keyless, free).

    Returns None on any failure so callers fall back to the static fixture;
    never raises into the request path.
    """
    url = (
        "https://api.open-meteo.com/v1/forecast"
        f"?latitude={lat}&longitude={lon}"
        "&current=precipitation,rain,weather_code,wind_gusts_10m"
        "&timezone=Asia%2FKolkata"
    )
    try:
        request = urllib.request.Request(url, headers={"User-Agent": "TrafficGrid/1.0"})
        with urllib.request.urlopen(
            request, timeout=WEATHER_TIMEOUT_SECONDS, context=_ssl_context()
        ) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, ValueError, OSError):
        return None

    current = payload.get("current") or {}
    rainfall = current.get("precipitation")
    if rainfall is None:
        rainfall = current.get("rain", 0.0)
    try:
        rainfall_mm_1h = max(0.0, float(rainfall))
    except (TypeError, ValueError):
        return None

    try:
        wind_gusts = round(float(current.get("wind_gusts_10m")), 1)
    except (TypeError, ValueError):
        wind_gusts = None

    return {
        "source": "open_meteo_live",
        "rainfall_mm_1h": round(rainfall_mm_1h, 2),
        "rainfall_intensity": _rain_intensity(rainfall_mm_1h),
        "flood_risk": round(min(1.0, rainfall_mm_1h / 25.0), 2),
        "road_surface": "wet" if rainfall_mm_1h > 0.1 else "dry",
        "wind_gusts_kmph": wind_gusts,
        "conditions": _weather_conditions(current.get("weather_code")),
        "observed_at": current.get("time") or now_ist().isoformat(),
        "location": {"latitude": round(float(lat), 4), "longitude": round(float(lon), 4)},
    }


def _fixture_weather() -> dict[str, Any]:
    return {
        "source": "weather_rain_flood_feed",
        "rainfall_mm_1h": 12.4,
        "rainfall_intensity": "moderate",
        "flood_risk": 0.42,
        "road_surface": "wet",
        "wind_gusts_kmph": None,
        "conditions": "rain",
        "observed_at": now_ist().isoformat(),
    }


def weather_for_point(lat: float, lon: float) -> dict[str, Any]:
    """Live current weather for a specific point, TTL-cached per coordinate."""
    global _LAST_WEATHER_MODE, _LAST_WEATHER_AT
    if not _live_weather_enabled():
        _LAST_WEATHER_MODE = "fixture"
        return _fixture_weather()

    try:
        key = f"{round(float(lat), 3)},{round(float(lon), 3)}"
    except (TypeError, ValueError):
        return _fixture_weather()

    now = time.monotonic()
    entry = _WEATHER_CACHE.get(key)
    if entry and entry.get("data") is not None and now - entry["fetched_at"] < WEATHER_TTL_SECONDS:
        _LAST_WEATHER_MODE, _LAST_WEATHER_AT = "live", entry["fetched_at"]
        return entry["data"]

    live = _fetch_live_weather(lat, lon)
    if live is not None:
        _WEATHER_CACHE[key] = {"fetched_at": now, "data": live}
        _LAST_WEATHER_MODE, _LAST_WEATHER_AT = "live", now
        return live

    # Network failed: reuse a recent live reading for this point, else fixture.
    if entry and entry.get("data") is not None:
        return entry["data"]
    _LAST_WEATHER_MODE = "fixture"
    return _fixture_weather()


def weather_feed() -> dict[str, Any]:
    # Operator-supplied override file wins, for offline demos / pinned scenarios.
    global _LAST_WEATHER_MODE
    configured = read_json_feed("weather.json")
    if configured:
        _LAST_WEATHER_MODE = "fixture_file"
        return configured[0]
    return weather_for_point(WEATHER_LAT, WEATHER_LON)


def sensor_counts() -> list[dict[str, Any]]:
    configured = read_json_feed("sensor_counts.json")
    if configured is not None:
        return configured

    return [
        {"sensor_id": "cctv-silk-board-north", "corridor": "Outer Ring Road", "vehicle_count_15m": 1382, "heavy_vehicle_share": 0.14},
        {"sensor_id": "anpr-mg-road-east", "corridor": "M G Road", "vehicle_count_15m": 754, "heavy_vehicle_share": 0.05},
        {"sensor_id": "cctv-hebbal-loop", "corridor": "Bellary Road", "vehicle_count_15m": 901, "heavy_vehicle_share": 0.09},
        {"sensor_id": "cctv-kasturba-road", "corridor": "Kasturba Road", "vehicle_count_15m": 618, "heavy_vehicle_share": 0.04},
    ]


def advisories() -> list[dict[str, Any]]:
    configured = read_json_feed("public_advisories.json")
    if configured is not None:
        return configured

    return [
        {
            "id": "advisory-mg-road-rain",
            "corridor": "M G Road",
            "message": "Expect slow movement near Trinity circle due to rainwater accumulation.",
            "severity": "high",
            "issued_at": iso_at(-22),
        },
        {
            "id": "advisory-silk-board-crash",
            "corridor": "Outer Ring Road",
            "message": "Use Hosur Road service lanes while response teams clear the crash scene.",
            "severity": "high",
            "issued_at": iso_at(-10),
        },
    ]


def officer_statuses() -> list[dict[str, Any]]:
    configured = read_json_feed("officer_status.json")
    if configured is not None:
        return configured

    return [
        {"officer_id": "CBP-214", "station": "Cubbon Park", "status": "available", "last_seen": iso_at(-3)},
        {"officer_id": "CBP-319", "station": "Cubbon Park", "status": "deployed", "last_seen": iso_at(-5)},
        {"officer_id": "MAD-102", "station": "Madiwala", "status": "available", "last_seen": iso_at(-4)},
        {"officer_id": "HBL-087", "station": "Hebbal", "status": "available", "last_seen": iso_at(-7)},
    ]


def speed_context_for_corridor(corridor: Any) -> dict[str, Any]:
    corridor_key = str(corridor or "").strip().lower()
    for row in gps_speed_feed():
        if str(row.get("corridor", "")).strip().lower() == corridor_key:
            free_flow = max(float(row.get("free_flow_speed_kmph") or 0), 1.0)
            current = max(float(row.get("current_speed_kmph") or 0), 1.0)
            return {
                **row,
                "speed_ratio": min(current / free_flow, 1.0),
                "delay_factor": max(free_flow / current - 1.0, 0.0),
            }
    return {
        "corridor": corridor,
        "current_speed_kmph": None,
        "free_flow_speed_kmph": None,
        "sample_size": 0,
        "speed_ratio": 0.65,
        "delay_factor": 0.35,
    }


def sensor_context_for_corridor(corridor: Any) -> dict[str, Any]:
    corridor_key = str(corridor or "").strip().lower()
    matching = [
        row
        for row in sensor_counts()
        if str(row.get("corridor", "")).strip().lower() == corridor_key
    ]
    if not matching:
        return {"vehicle_count_15m": 0, "heavy_vehicle_share": 0.0, "sensor_count": 0}
    vehicle_count = sum(int(row.get("vehicle_count_15m") or 0) for row in matching)
    heavy_share = sum(float(row.get("heavy_vehicle_share") or 0.0) for row in matching) / len(matching)
    return {
        "vehicle_count_15m": vehicle_count,
        "heavy_vehicle_share": heavy_share,
        "sensor_count": len(matching),
    }


def _event_lat_lon(event_features: dict[str, Any]) -> tuple[float, float] | None:
    try:
        lat = float(event_features.get("latitude"))
        lon = float(event_features.get("longitude"))
    except (TypeError, ValueError):
        return None
    if lat == 0.0 and lon == 0.0:
        return None
    return lat, lon


def operational_context_for_event(event_features: dict[str, Any]) -> dict[str, Any]:
    corridor = event_features.get("corridor")
    speed = speed_context_for_corridor(corridor)
    sensor = sensor_context_for_corridor(corridor)
    # Live weather at the incident's own location when we have coordinates.
    point = _event_lat_lon(event_features)
    weather = weather_for_point(*point) if point else weather_feed()
    relevant_advisories = [
        advisory
        for advisory in advisories()
        if str(advisory.get("corridor", "")).strip().lower() == str(corridor or "").strip().lower()
    ]
    return {
        "speed": speed,
        "weather": weather,
        "sensors": sensor,
        "advisories": relevant_advisories,
    }


def all_feed_records() -> dict[str, Any]:
    return {
        "live_incidents": live_incidents(),
        "planned_permits": planned_permits(),
        "gps_speeds": gps_speed_feed(),
        "weather": weather_feed(),
        "sensor_counts": sensor_counts(),
        "officer_statuses": officer_statuses(),
        "public_advisories": advisories(),
    }


def integration_status() -> list[dict[str, Any]]:
    observed = datetime.now(UTC)
    # Refresh weather so its reported mode (live vs fixture) is current.
    weather_feed()
    weather_mode = "live_api" if _LAST_WEATHER_MODE == "live" else "local_adapter"
    weather_age = int(max(0.0, time.monotonic() - _LAST_WEATHER_AT)) if _LAST_WEATHER_AT else 0
    feeds = [
        ("ASTraM live incidents", "incident", "local_adapter", len(live_incidents()), 0),
        ("Planned permits", "permit", "local_adapter", len(planned_permits()), 0),
        ("Fleet GPS speeds", "mobility", "local_adapter", len(gps_speed_feed()), 0),
        ("Weather/rain/flooding", "weather", weather_mode, 1, weather_age),
        ("CCTV/ANPR counts", "sensor", "local_adapter", len(sensor_counts()), 0),
        ("Officer mobile status", "field", "local_adapter", len(officer_statuses()), 0),
        ("Public advisories", "advisory", "local_adapter", len(advisories()), 0),
    ]
    return [
        FeedStatus(
            name=name,
            category=category,
            mode=mode,
            records=records,
            last_seen=observed.isoformat(),
            freshness_seconds=freshness,
        ).as_dict()
        for name, category, mode, records, freshness in feeds
    ]
