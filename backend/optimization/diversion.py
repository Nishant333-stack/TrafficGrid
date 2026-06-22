from __future__ import annotations

import math
import os
import time
from pathlib import Path
from typing import Any

import networkx as nx
import pandas as pd

from backend.geo.geo_utils import edge_names, haversine_meters, nearest_node_by_haversine, node_lat_lon
from backend.geo.road_graph import get_graph


MODEL_DIR = Path(__file__).with_name("models")
HOUR_BUCKET_SIZE = 3
CONGESTION_RISK_WEIGHT = 2.8
EVENT_CORRIDOR_PENALTY = 1.35
DEFAULT_DIVERSION_TIME_BUDGET_SECONDS = float(os.environ.get("DIVERSION_TIME_BUDGET_SECONDS", "6.0"))


def hour_bucket(hour_of_day: int) -> int:
    if hour_of_day < 0:
        return -1
    return int(hour_of_day // HOUR_BUCKET_SIZE * HOUR_BUCKET_SIZE)


def event_time_parts(event_features: dict[str, Any]) -> tuple[int, int]:
    timestamp = pd.to_datetime(event_features.get("start_datetime"), errors="coerce", utc=True)
    if pd.isna(timestamp):
        return -1, -1
    return int(timestamp.hour), int(timestamp.dayofweek)


def load_risk_density() -> pd.DataFrame:
    path = MODEL_DIR / "risk_density.parquet"
    if not path.exists():
        return pd.DataFrame(columns=["corridor", "hour_bucket", "day_of_week", "risk_score"])
    return pd.read_parquet(path)


def risk_for_corridor(
    risk_density: pd.DataFrame,
    corridor: str,
    bucket: int,
    day_of_week: int,
) -> float:
    if risk_density.empty or not corridor:
        return 0.0
    exact = risk_density.loc[
        (risk_density["corridor"].astype(str) == str(corridor))
        & (risk_density["hour_bucket"] == bucket)
        & (risk_density["day_of_week"] == day_of_week)
    ]
    if not exact.empty:
        return float(exact["risk_score"].iloc[0])
    corridor_rows = risk_density.loc[risk_density["corridor"].astype(str) == str(corridor)]
    if corridor_rows.empty:
        return 0.0
    return float(corridor_rows["risk_score"].mean())


def clamp(value: float, lower: float, upper: float) -> float:
    return max(lower, min(upper, value))


def incident_corridors(graph: nx.MultiDiGraph, node_id: int) -> set[str]:
    names: set[str] = set()
    edge_iterators = [graph.out_edges(node_id, data=True)]
    if graph.is_directed():
        edge_iterators.append(graph.in_edges(node_id, data=True))

    for iterator in edge_iterators:
        for _, _, data in iterator:
            names.update(edge_names(data.get("name")))
            names.update(edge_names(data.get("ref")))
    return names


def edge_corridors(data: dict[str, Any]) -> set[str]:
    names: set[str] = set()
    names.update(edge_names(data.get("name")))
    names.update(edge_names(data.get("ref")))
    return names


def edge_base_length_m(
    graph: nx.MultiDiGraph,
    source: int,
    target: int,
    data: dict[str, Any],
) -> float:
    length = data.get("length")
    if length is not None:
        return max(float(length), 1.0)
    return max(haversine_meters(*node_lat_lon(graph, source), *node_lat_lon(graph, target)), 1.0)


def edge_risk_score(
    data: dict[str, Any],
    risk_density: pd.DataFrame,
    event_features: dict[str, Any],
    bucket: int,
    day_of_week: int,
) -> float:
    risks = [
        risk_for_corridor(risk_density, corridor, bucket, day_of_week)
        for corridor in edge_corridors(data)
    ]
    if risks:
        return clamp(max(risks), 0.0, 1.0)

    event_corridor = str(event_features.get("corridor") or "").strip().lower()
    road_name = " ".join(edge_corridors(data)).lower()
    if event_corridor and event_corridor in road_name:
        return clamp(float(event_features.get("risk_score") or 0.0), 0.0, 1.0)
    return 0.0


def congestion_weight_function(
    graph: nx.MultiDiGraph,
    risk_density: pd.DataFrame,
    event_features: dict[str, Any],
):
    hour_of_day, day_of_week = event_time_parts(event_features)
    bucket = hour_bucket(hour_of_day)
    event_corridor = str(event_features.get("corridor") or "").strip().lower()

    def single_edge_cost(source: int, target: int, data: dict[str, Any]) -> float:
        base_length = edge_base_length_m(graph, source, target, data)
        risk = edge_risk_score(data, risk_density, event_features, bucket, day_of_week)
        multiplier = 1.0 + risk * CONGESTION_RISK_WEIGHT
        edge_names_text = " ".join(edge_corridors(data)).lower()
        if event_corridor and event_corridor in edge_names_text:
            multiplier += EVENT_CORRIDOR_PENALTY
        return base_length * multiplier

    def weight(source: int, target: int, data: dict[str, Any]) -> float:
        if "length" in data:
            return single_edge_cost(source, target, data)
        edge_options = [
            single_edge_cost(source, target, attrs)
            for attrs in data.values()
            if isinstance(attrs, dict)
        ]
        if edge_options:
            return min(edge_options)
        return max(haversine_meters(*node_lat_lon(graph, source), *node_lat_lon(graph, target)), 1.0)

    return weight


def route_has_high_risk_node(
    graph: nx.MultiDiGraph,
    route: list[int],
    risk_density: pd.DataFrame,
    event_features: dict[str, Any],
    threshold: float = 0.8,
) -> bool:
    hour_of_day, day_of_week = event_time_parts(event_features)
    bucket = hour_bucket(hour_of_day)
    for node_id in route:
        for corridor in incident_corridors(graph, node_id):
            if risk_for_corridor(risk_density, corridor, bucket, day_of_week) > threshold:
                return True
    return False


def path_length_m(graph: nx.MultiDiGraph, route: list[int]) -> float:
    length = 0.0
    for source, target in zip(route, route[1:]):
        edge_data = graph.get_edge_data(source, target, default={})
        if not edge_data:
            length += haversine_meters(*node_lat_lon(graph, source), *node_lat_lon(graph, target))
            continue
        length += min(float(data.get("length") or 0.0) for data in edge_data.values())
    return length


def path_congestion_cost(
    graph: nx.MultiDiGraph,
    route: list[int],
    risk_density: pd.DataFrame,
    event_features: dict[str, Any],
) -> float:
    weight = congestion_weight_function(graph, risk_density, event_features)
    cost = 0.0
    for source, target in zip(route, route[1:]):
        edge_data = graph.get_edge_data(source, target, default={})
        cost += float(weight(source, target, edge_data))
    return cost


def route_max_risk(
    graph: nx.MultiDiGraph,
    route: list[int],
    risk_density: pd.DataFrame,
    event_features: dict[str, Any],
) -> float:
    hour_of_day, day_of_week = event_time_parts(event_features)
    bucket = hour_bucket(hour_of_day)
    max_risk = 0.0
    for source, target in zip(route, route[1:]):
        edge_data = graph.get_edge_data(source, target, default={})
        if "length" in edge_data:
            max_risk = max(max_risk, edge_risk_score(edge_data, risk_density, event_features, bucket, day_of_week))
            continue
        for attrs in edge_data.values():
            if isinstance(attrs, dict):
                max_risk = max(max_risk, edge_risk_score(attrs, risk_density, event_features, bucket, day_of_week))
    return clamp(max_risk, 0.0, 1.0)


def route_quality_score(
    original_length: float,
    alternate_length: float,
    original_cost: float,
    alternate_cost: float,
    max_risk: float,
) -> float:
    added_ratio = max(0.0, alternate_length - original_length) / max(original_length, 1.0)
    cost_improvement = max(0.0, original_cost - alternate_cost) / max(original_cost, 1.0)
    return round(clamp(0.72 + cost_improvement * 0.35 - added_ratio * 0.4 - max_risk * 0.28, 0.0, 1.0), 3)


def remove_event_edges(
    graph: nx.MultiDiGraph,
    event_node: int,
    event_lat: float,
    event_lon: float,
    block_radius_m: float = 120.0,
) -> nx.MultiDiGraph:
    modified = graph.copy()
    nearby_nodes = [
        node_id
        for node_id in graph.nodes
        if haversine_meters(event_lat, event_lon, *node_lat_lon(graph, node_id)) <= block_radius_m
    ]
    if event_node not in nearby_nodes:
        nearby_nodes.append(event_node)

    edges_to_remove: set[tuple[int, int, int]] = set()
    for node_id in nearby_nodes:
        for source, target, key in graph.out_edges(node_id, keys=True):
            edges_to_remove.add((source, target, key))
        if graph.is_directed():
            for source, target, key in graph.in_edges(node_id, keys=True):
                edges_to_remove.add((source, target, key))

    modified.remove_edges_from(edges_to_remove)
    return modified


def angle_from_event(graph: nx.MultiDiGraph, node_id: int, event_lat: float, event_lon: float) -> float:
    node_lat, node_lon = node_lat_lon(graph, node_id)
    return math.atan2(node_lat - event_lat, node_lon - event_lon)


def endpoint_pairs(
    graph: nx.MultiDiGraph,
    event_node: int,
    event_lat: float,
    event_lon: float,
    target_distance_m: float = 1500.0,
    max_pairs: int = 8,
) -> list[tuple[int, int]]:
    pairs: list[tuple[int, int]] = []

    def add_pair(source: int, target: int) -> None:
        if source == target:
            return
        pair = (source, target)
        reverse_pair = (target, source)
        if pair not in pairs and reverse_pair not in pairs:
            pairs.append(pair)

    neighbor_nodes: set[int] = set()
    for _, target in graph.out_edges(event_node):
        neighbor_nodes.add(target)
    if graph.is_directed():
        for source, _ in graph.in_edges(event_node):
            neighbor_nodes.add(source)

    ordered_neighbors = sorted(
        neighbor_nodes,
        key=lambda node_id: haversine_meters(
            *node_lat_lon(graph, event_node),
            *node_lat_lon(graph, node_id),
        ),
    )[:16]
    neighbor_pair_cap = min(2, max_pairs)
    for index, source in enumerate(ordered_neighbors):
        source_angle = angle_from_event(graph, source, event_lat, event_lon)
        for target in ordered_neighbors[index + 1:]:
            angle_delta = abs(math.atan2(
                math.sin(source_angle - angle_from_event(graph, target, event_lat, event_lon)),
                math.cos(source_angle - angle_from_event(graph, target, event_lat, event_lon)),
            ))
            if angle_delta >= math.radians(90):
                add_pair(source, target)
            if len(pairs) >= neighbor_pair_cap:
                break
        if len(pairs) >= neighbor_pair_cap:
            break

    search_graph = graph.to_undirected(as_view=True)
    lengths = nx.single_source_dijkstra_path_length(
        search_graph,
        event_node,
        cutoff=target_distance_m * 1.45,
        weight="length",
    )
    candidates = [
        node_id
        for node_id, distance in lengths.items()
        if target_distance_m * 0.65 <= distance <= target_distance_m * 1.45
    ]
    candidates.sort(
        key=lambda node_id: abs(lengths[node_id] - target_distance_m)
    )
    candidates = candidates[:16]

    for index, source in enumerate(candidates):
        source_angle = angle_from_event(graph, source, event_lat, event_lon)
        for target in candidates[index + 1:]:
            angle_delta = abs(math.atan2(
                math.sin(source_angle - angle_from_event(graph, target, event_lat, event_lon)),
                math.cos(source_angle - angle_from_event(graph, target, event_lat, event_lon)),
            ))
            if angle_delta < math.radians(120):
                continue
            add_pair(source, target)
            if len(pairs) >= max_pairs:
                return pairs
    return pairs


def _best_edge_data(graph: nx.MultiDiGraph, source: int, target: int) -> dict[str, Any] | None:
    """Return the shortest parallel edge between two nodes (the one routing uses)."""
    data = graph.get_edge_data(source, target)
    if not data:
        return None
    return min(data.values(), key=lambda edge: edge.get("length", float("inf")))


def route_coordinates(graph: nx.MultiDiGraph, route: list[int]) -> list[dict[str, float]]:
    """Trace the route along real road geometry.

    For each segment we follow the OSM edge's ``geometry`` (a curved LineString)
    when present, instead of drawing a straight chord between intersections.
    Edges without geometry (already-straight segments) fall back to the two
    node endpoints. Consecutive duplicate points are collapsed.
    """
    coords: list[tuple[float, float]] = []

    def push(lat: float, lon: float) -> None:
        point = (round(float(lat), 7), round(float(lon), 7))
        if not coords or coords[-1] != point:
            coords.append(point)

    if not route:
        return []

    if len(route) == 1:
        lat, lon = node_lat_lon(graph, route[0])
        return [{"lat": lat, "lon": lon}]

    for index in range(len(route) - 1):
        source = route[index]
        target = route[index + 1]
        edge = _best_edge_data(graph, source, target)
        geometry = edge.get("geometry") if edge else None
        if geometry is not None and getattr(geometry, "coords", None) is not None:
            # Shapely LineString stores (x=lon, y=lat); orient it source -> target.
            line = [(lat, lon) for lon, lat in geometry.coords]
            src_lat, src_lon = node_lat_lon(graph, source)
            if line and haversine_meters(*line[0], src_lat, src_lon) > haversine_meters(
                *line[-1], src_lat, src_lon
            ):
                line.reverse()
            for lat, lon in line:
                push(lat, lon)
        else:
            src_lat, src_lon = node_lat_lon(graph, source)
            tgt_lat, tgt_lon = node_lat_lon(graph, target)
            push(src_lat, src_lon)
            push(tgt_lat, tgt_lon)

    return [{"lat": lat, "lon": lon} for lat, lon in coords]


def geographic_heuristic(graph: nx.MultiDiGraph):
    def heuristic(left: int, right: int) -> float:
        return haversine_meters(*node_lat_lon(graph, left), *node_lat_lon(graph, right))

    return heuristic


def compute_diversions(
    event_lat: float,
    event_lon: float,
    event_features: dict[str, Any],
    graph: nx.MultiDiGraph | None = None,
    limit: int = 3,
    max_runtime_seconds: float = DEFAULT_DIVERSION_TIME_BUDGET_SECONDS,
) -> list[dict[str, Any]]:
    started = time.perf_counter()

    def expired() -> bool:
        return time.perf_counter() - started >= max_runtime_seconds

    road_graph = graph or get_graph()
    event_node = nearest_node_by_haversine(road_graph, event_lat, event_lon)
    risk_density = load_risk_density()
    pairs = endpoint_pairs(
        road_graph,
        event_node,
        event_lat,
        event_lon,
        max_pairs=max(limit * 2, 4),
    )
    congestion_weight = congestion_weight_function(road_graph, risk_density, event_features)
    heuristic = geographic_heuristic(road_graph)

    diversions: list[dict[str, Any]] = []
    seen_paths: set[tuple[int, ...]] = set()
    for block_radius_m in (120.0, 240.0, 400.0):
        if expired():
            break
        modified_graph = remove_event_edges(
            road_graph,
            event_node,
            event_lat,
            event_lon,
            block_radius_m=block_radius_m,
        )
        for source, target in pairs:
            if expired():
                break
            route_pair: tuple[list[int], list[int], int, int] | None = None
            for candidate_source, candidate_target in ((source, target), (target, source)):
                if expired():
                    break
                try:
                    original_route = nx.astar_path(
                        road_graph,
                        candidate_source,
                        candidate_target,
                        heuristic=heuristic,
                        weight="length",
                    )
                    alternate_route = nx.astar_path(
                        modified_graph,
                        candidate_source,
                        candidate_target,
                        heuristic=heuristic,
                        weight=congestion_weight,
                    )
                except (nx.NetworkXNoPath, nx.NodeNotFound):
                    continue
                route_pair = (
                    original_route,
                    alternate_route,
                    candidate_source,
                    candidate_target,
                )
                break
            if route_pair is None:
                continue

            original_route, alternate_route, actual_source, actual_target = route_pair

            if alternate_route == original_route:
                continue
            if event_node in alternate_route:
                continue
            path_key = tuple(int(node_id) for node_id in alternate_route)
            if path_key in seen_paths:
                continue
            max_route_risk = route_max_risk(road_graph, alternate_route, risk_density, event_features)
            if max_route_risk > 0.95:
                continue

            seen_paths.add(path_key)
            original_length = path_length_m(road_graph, original_route)
            alternate_length = path_length_m(road_graph, alternate_route)
            original_cost = path_congestion_cost(
                road_graph,
                original_route,
                risk_density,
                event_features,
            )
            alternate_cost = path_congestion_cost(
                road_graph,
                alternate_route,
                risk_density,
                event_features,
            )
            diversions.append(
                {
                    "source_node_id": int(actual_source),
                    "target_node_id": int(actual_target),
                    "original_length_m": original_length,
                    "diversion_length_m": alternate_length,
                    "added_length_m": max(0.0, alternate_length - original_length),
                    "original_congestion_cost": original_cost,
                    "diversion_congestion_cost": alternate_cost,
                    "congestion_cost_delta": original_cost - alternate_cost,
                    "max_corridor_risk": max_route_risk,
                    "route_quality_score": route_quality_score(
                        original_length,
                        alternate_length,
                        original_cost,
                        alternate_cost,
                        max_route_risk,
                    ),
                    "block_radius_m": block_radius_m,
                    "path_node_ids": [int(node_id) for node_id in alternate_route],
                    "path": route_coordinates(road_graph, alternate_route),
                }
            )

            if len(diversions) >= limit:
                break
        if len(diversions) >= limit:
            break

    if not diversions and pairs:
        for source, target in pairs:
            if expired():
                break
            route_pair: tuple[list[int], int, int] | None = None
            for candidate_source, candidate_target in ((source, target), (target, source)):
                try:
                    route = nx.astar_path(
                        road_graph,
                        candidate_source,
                        candidate_target,
                        heuristic=heuristic,
                        weight=congestion_weight,
                    )
                except (nx.NetworkXNoPath, nx.NodeNotFound):
                    continue
                route_pair = (route, candidate_source, candidate_target)
                break
            if route_pair is None:
                continue

            route, actual_source, actual_target = route_pair
            if event_node in route:
                continue
            path_key = tuple(int(node_id) for node_id in route)
            if path_key in seen_paths:
                continue

            max_route_risk = route_max_risk(road_graph, route, risk_density, event_features)
            if max_route_risk > 0.95:
                continue
            route_length = path_length_m(road_graph, route)
            route_cost = path_congestion_cost(
                road_graph,
                route,
                risk_density,
                event_features,
            )
            seen_paths.add(path_key)
            diversions.append(
                {
                    "source_node_id": int(actual_source),
                    "target_node_id": int(actual_target),
                    "original_length_m": route_length,
                    "diversion_length_m": route_length,
                    "added_length_m": 0.0,
                    "original_congestion_cost": route_cost,
                    "diversion_congestion_cost": route_cost,
                    "congestion_cost_delta": 0.0,
                    "max_corridor_risk": max_route_risk,
                    "route_quality_score": round(clamp(0.58 - max_route_risk * 0.2, 0.25, 0.58), 3),
                    "block_radius_m": 0.0,
                    "fallback_reason": "advisory_alternate_corridor",
                    "path_node_ids": [int(node_id) for node_id in route],
                    "path": route_coordinates(road_graph, route),
                }
            )
            if len(diversions) >= limit:
                break

    diversions.sort(
        key=lambda item: (
            -float(item.get("route_quality_score") or 0.0),
            float(item.get("max_corridor_risk") or 0.0),
            float(item["added_length_m"]),
        )
    )
    for rank, diversion in enumerate(diversions[:limit], start=1):
        diversion["rank"] = rank
    return diversions[:limit]
