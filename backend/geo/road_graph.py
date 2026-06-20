from __future__ import annotations

import os
import pickle
import time
import math
from pathlib import Path
from typing import Any

import networkx as nx

from backend.config.env_loader import load_project_env
from backend.geo.geo_utils import haversine_meters, nearest_node_by_haversine, node_lat_lon


load_project_env()


DEFAULT_BBOX = {
    "north": 13.08,
    "south": 12.86,
    "east": 77.78,
    "west": 77.45,
}
DEFAULT_CACHE_PATH = Path(__file__).with_name("graph_cache") / "bengaluru_drive_graph.pkl"
DEMO_CACHE_PATH = Path(__file__).with_name("graph_cache") / "bengaluru_demo_graph.pkl"
LOCAL_CACHE_DIR = Path(__file__).with_name("graph_cache") / "local"
BBOX_TOLERANCE_DEGREES = 0.002
_GRAPH_MEMORY_CACHE: dict[Path, tuple[float, nx.MultiDiGraph]] = {}
_GRAPH_CACHE_METRICS = {
    "requests": 0,
    "memory_hits": 0,
    "disk_hits": 0,
    "downloads": 0,
    "stale_fallbacks": 0,
    "misses": 0,
    "load_seconds_total": 0.0,
}


def parse_bbox(value: str | None = None) -> dict[str, float]:
    raw = value or os.environ.get("BENGALURU_BBOX")
    if not raw:
        return DEFAULT_BBOX.copy()

    parts = [part.strip() for part in raw.split(",")]
    if len(parts) != 4:
        raise ValueError(
            "BENGALURU_BBOX must be either 'south,west,north,east' "
            "or 'north,south,east,west'"
        )

    first, second, third, fourth = (float(part) for part in parts)

    # Prefer the common geospatial order used in the project .env:
    # south,west,north,east. Keep backward compatibility with the
    # earlier north,south,east,west convention used by the first script.
    if first < third and second < fourth:
        south, west, north, east = first, second, third, fourth
    else:
        north, south, east, west = first, second, third, fourth

    if not (south < north and west < east):
        raise ValueError(
            "Invalid BENGALURU_BBOX: expected south<north and west<east"
        )
    return {"north": north, "south": south, "east": east, "west": west}


def graph_cache_path(cache_path: str | Path | None = None) -> Path:
    path = Path(
        cache_path
        or os.environ.get("BENGALURU_GRAPH_CACHE")
        or DEFAULT_CACHE_PATH
    )
    if path.exists() and path.is_dir():
        return path / DEFAULT_CACHE_PATH.name
    if path.suffix:
        return path
    return path / DEFAULT_CACHE_PATH.name


def bbox_around_point(lat: float, lon: float, radius_m: float) -> dict[str, float]:
    lat_delta = radius_m / 111_320.0
    lon_delta = radius_m / max(111_320.0 * math.cos(math.radians(lat)), 1.0)
    return {
        "north": lat + lat_delta,
        "south": lat - lat_delta,
        "east": lon + lon_delta,
        "west": lon - lon_delta,
    }


def local_graph_cache_path(lat: float, lon: float, radius_m: float) -> Path:
    tile_lat = round(lat, 2)
    tile_lon = round(lon, 2)
    radius_key = int(round(radius_m / 500.0) * 500)
    return LOCAL_CACHE_DIR / f"drive_{tile_lat:.2f}_{tile_lon:.2f}_{radius_key}m.pkl"


def normalized_cache_path(path: Path) -> Path:
    return path.expanduser().resolve(strict=False)


def record_cache_metric(name: str, amount: float = 1.0) -> None:
    _GRAPH_CACHE_METRICS[name] = _GRAPH_CACHE_METRICS.get(name, 0) + amount


def reset_graph_cache_metrics() -> None:
    for key in list(_GRAPH_CACHE_METRICS):
        _GRAPH_CACHE_METRICS[key] = 0.0 if key == "load_seconds_total" else 0


def graph_cache_metrics() -> dict[str, Any]:
    requests = int(_GRAPH_CACHE_METRICS.get("requests", 0))
    memory_hits = int(_GRAPH_CACHE_METRICS.get("memory_hits", 0))
    disk_hits = int(_GRAPH_CACHE_METRICS.get("disk_hits", 0))
    downloads = int(_GRAPH_CACHE_METRICS.get("downloads", 0))
    stale_fallbacks = int(_GRAPH_CACHE_METRICS.get("stale_fallbacks", 0))
    served_from_cache = max(0, requests - downloads)
    return {
        "requests": requests,
        "memory_hits": memory_hits,
        "disk_hits": disk_hits,
        "downloads": downloads,
        "stale_fallbacks": stale_fallbacks,
        "misses": int(_GRAPH_CACHE_METRICS.get("misses", 0)),
        "cache_hit_rate": round(served_from_cache / max(requests, 1), 3),
        "memory_hit_rate": round(memory_hits / max(requests, 1), 3),
        "load_seconds_total": round(float(_GRAPH_CACHE_METRICS.get("load_seconds_total", 0.0)), 4),
    }


def bbox_covers(
    cached_bbox: dict[str, float] | None,
    requested_bbox: dict[str, float],
    tolerance: float = BBOX_TOLERANCE_DEGREES,
) -> bool:
    if not cached_bbox:
        return False
    try:
        return (
            float(cached_bbox["north"]) + tolerance >= requested_bbox["north"]
            and float(cached_bbox["south"]) - tolerance <= requested_bbox["south"]
            and float(cached_bbox["east"]) + tolerance >= requested_bbox["east"]
            and float(cached_bbox["west"]) - tolerance <= requested_bbox["west"]
        )
    except (KeyError, TypeError, ValueError):
        return False


def refresh_stale_graph_enabled() -> bool:
    return os.environ.get("BENGALURU_REFRESH_STALE_GRAPH", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "y",
    }


def mark_stale_graph(
    graph: nx.MultiDiGraph,
    requested_bbox: dict[str, float],
    message: str,
) -> nx.MultiDiGraph:
    record_cache_metric("stale_fallbacks")
    graph.graph["cache_status"] = "stale_fallback"
    graph.graph["requested_bbox"] = requested_bbox
    graph.graph["cache_warning"] = message
    return graph


def _download_osm_graph(bbox: dict[str, float]) -> nx.MultiDiGraph:
    try:
        import osmnx as ox
    except ImportError as exc:
        raise RuntimeError(
            "osmnx is required to download the Bengaluru road graph. "
            "Install dependencies with: pip install -r requirements.txt"
        ) from exc

    ox.settings.use_cache = True
    ox.settings.log_console = False
    north = bbox["north"]
    south = bbox["south"]
    east = bbox["east"]
    west = bbox["west"]

    try:
        graph = ox.graph_from_bbox(
            (west, south, east, north),
            network_type="drive",
            simplify=True,
            retain_all=False,
            truncate_by_edge=True,
        )
    except TypeError:
        graph = ox.graph_from_bbox(
            north,
            south,
            east,
            west,
            network_type="drive",
            simplify=True,
            retain_all=False,
            truncate_by_edge=True,
        )

    graph.graph["bbox"] = bbox
    graph.graph["source"] = "osmnx"
    return graph


def _save_graph(graph: nx.MultiDiGraph, cache_path: Path) -> None:
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    with cache_path.open("wb") as handle:
        pickle.dump(graph, handle, protocol=pickle.HIGHEST_PROTOCOL)
    resolved = normalized_cache_path(cache_path)
    _GRAPH_MEMORY_CACHE[resolved] = (cache_path.stat().st_mtime, graph)


def _load_graph(cache_path: Path) -> nx.MultiDiGraph:
    started = time.perf_counter()
    resolved = normalized_cache_path(cache_path)
    mtime = cache_path.stat().st_mtime
    cached = _GRAPH_MEMORY_CACHE.get(resolved)
    if cached and cached[0] == mtime:
        record_cache_metric("memory_hits")
        record_cache_metric("load_seconds_total", time.perf_counter() - started)
        return cached[1]

    with cache_path.open("rb") as handle:
        graph = pickle.load(handle)
    _GRAPH_MEMORY_CACHE[resolved] = (mtime, graph)
    record_cache_metric("disk_hits")
    record_cache_metric("load_seconds_total", time.perf_counter() - started)
    return graph


def get_graph(
    bbox: dict[str, float] | None = None,
    cache_path: str | Path | None = None,
    force_download: bool = False,
) -> nx.MultiDiGraph:
    """Return a cached drivable Bengaluru OSM graph, downloading only on cache miss."""

    if os.environ.get("ROAD_GRAPH_MODE", "").lower() in {"demo", "1", "true", "yes"}:
        demo_path = cache_demo_graph()
        record_cache_metric("requests")
        graph = _load_graph(demo_path)
        graph.graph["cache_status"] = "fresh"
        graph.graph["route_graph_scope"] = "demo"
        return graph

    record_cache_metric("requests")
    path = graph_cache_path(cache_path)
    requested_bbox = bbox or parse_bbox()
    stale_graph: nx.MultiDiGraph | None = None

    if path.exists():
        graph = _load_graph(path)
        source = graph.graph.get("source")
        cached_bbox = graph.graph.get("bbox")
        if not force_download and source != "offline_demo" and bbox_covers(cached_bbox, requested_bbox):
            graph.graph["cache_status"] = "fresh"
            return graph
        stale_graph = graph
        if not force_download and not refresh_stale_graph_enabled():
            return mark_stale_graph(
                stale_graph,
                requested_bbox,
                (
                    "Cached road graph does not cover the requested Bengaluru bbox. "
                    "Run get_graph(force_download=True) or set BENGALURU_REFRESH_STALE_GRAPH=true "
                    "to rebuild the cache."
                ),
            )

    record_cache_metric("misses")
    try:
        graph = _download_osm_graph(requested_bbox)
    except Exception as exc:
        if stale_graph is None:
            raise
        return mark_stale_graph(
            stale_graph,
            requested_bbox,
            (
                "Cached road graph does not cover the requested Bengaluru bbox, "
                f"and refresh failed: {exc}"
            ),
        )

    graph.graph["cache_status"] = "fresh"
    record_cache_metric("downloads")
    _save_graph(graph, path)
    return graph


def graph_nearest_distance_m(graph: nx.MultiDiGraph, lat: float, lon: float) -> float:
    if not graph.nodes:
        return float("inf")
    nearest = nearest_node_by_haversine(graph, lat, lon)
    return haversine_meters(lat, lon, *node_lat_lon(graph, nearest))


def get_graph_for_point(
    lat: float,
    lon: float,
    radius_m: float = 3_000.0,
    max_nearest_distance_m: float = 900.0,
) -> nx.MultiDiGraph:
    """Return the best cached road graph for a specific event location.

    The city-wide cache is preferred when it actually covers the event. If the
    nearest graph node is too far away, a small event-local OSM graph is cached
    by location tile so future forecasts do not re-download it.
    """

    city_graph = get_graph()
    if graph_nearest_distance_m(city_graph, lat, lon) <= max_nearest_distance_m:
        if city_graph.graph.get("cache_status") == "stale_fallback":
            city_graph.graph["cache_status"] = "fresh_for_event"
            city_graph.graph.pop("cache_warning", None)
        city_graph.graph["route_graph_scope"] = "city"
        return city_graph

    local_bbox = bbox_around_point(lat, lon, radius_m)
    path = local_graph_cache_path(lat, lon, radius_m)
    record_cache_metric("requests")
    stale_graph: nx.MultiDiGraph | None = None

    if path.exists():
        graph = _load_graph(path)
        cached_bbox = graph.graph.get("bbox")
        if bbox_covers(cached_bbox, local_bbox):
            graph.graph["cache_status"] = "fresh"
            graph.graph["route_graph_scope"] = "event_local"
            return graph
        stale_graph = graph

    record_cache_metric("misses")
    try:
        graph = _download_osm_graph(local_bbox)
    except Exception as exc:
        if stale_graph is not None:
            stale_graph.graph["route_graph_scope"] = "event_local_stale"
            return mark_stale_graph(
                stale_graph,
                local_bbox,
                f"Event-local road graph refresh failed: {exc}",
            )
        city_graph.graph["route_graph_scope"] = "city_stale_for_event"
        return mark_stale_graph(
            city_graph,
            local_bbox,
            (
                "No event-local road graph was available, and refresh failed: "
                f"{exc}"
            ),
        )

    graph.graph["cache_status"] = "fresh"
    graph.graph["route_graph_scope"] = "event_local"
    record_cache_metric("downloads")
    _save_graph(graph, path)
    return graph


def cache_demo_graph(cache_path: str | Path | None = None) -> Path:
    """Create a tiny Bengaluru-shaped graph for offline tests when OSM is unavailable."""

    path = Path(cache_path) if cache_path else DEMO_CACHE_PATH
    if path.exists():
        return path

    center_lat = 12.9716
    center_lon = 77.5946
    step = 0.0045
    graph = nx.MultiDiGraph()
    graph.graph["crs"] = "EPSG:4326"
    graph.graph["source"] = "offline_demo"

    node_id = 1
    grid: dict[tuple[int, int], int] = {}
    for row in range(-3, 4):
        for col in range(-3, 4):
            grid[(row, col)] = node_id
            graph.add_node(
                node_id,
                y=center_lat + row * step,
                x=center_lon + col * step,
            )
            node_id += 1

    for (row, col), source in grid.items():
        for d_row, d_col, name in [
            (0, 1, "M G Road"),
            (1, 0, "Outer Ring Road"),
        ]:
            target = grid.get((row + d_row, col + d_col))
            if target is None:
                continue
            highway = "primary" if row == 0 or col == 0 else "secondary"
            length = 500.0
            attrs = {"length": length, "highway": highway, "name": name}
            graph.add_edge(source, target, **attrs)
            graph.add_edge(target, source, **attrs)

    _save_graph(graph, path)
    return path
