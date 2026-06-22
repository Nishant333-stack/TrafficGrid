#!/usr/bin/env python3
"""Download the real Bengaluru drivable OSM graph and cache it.

Run at Docker build time so the deployed app routes diversions over the actual
street network instead of the offline demo grid, with no runtime download.

The bbox comes from BENGALURU_BBOX (or the project default in road_graph.py).
ROAD_GRAPH_MODE is forced off here so we always fetch the real graph, even when
the image sets ROAD_GRAPH_MODE for runtime.
"""
from __future__ import annotations

import os
import sys
import time


def main() -> int:
    # Allow a fast, real-graph-free build: ROAD_GRAPH_MODE=demo or SKIP_GRAPH_BUILD.
    mode = os.environ.get("ROAD_GRAPH_MODE", "").strip().lower()
    skip = os.environ.get("SKIP_GRAPH_BUILD", "").strip().lower() in {"1", "true", "yes"}
    if skip or mode in {"demo", "1", "true", "yes"}:
        print("Skipping real road graph build (demo mode / SKIP_GRAPH_BUILD set).", flush=True)
        return 0

    # Ensure we hit the real OSM download path, not the demo grid.
    os.environ.pop("ROAD_GRAPH_MODE", None)

    from backend.geo.road_graph import get_graph, graph_cache_path, parse_bbox

    bbox = parse_bbox()
    target = graph_cache_path()
    print(f"Building real road graph for bbox={bbox}", flush=True)
    print(f"  -> {target}", flush=True)

    started = time.perf_counter()
    attempts = int(os.environ.get("GRAPH_BUILD_ATTEMPTS", "3"))
    graph = None
    for attempt in range(1, attempts + 1):
        try:
            graph = get_graph(force_download=True)
            break
        except Exception as exc:  # pragma: no cover - build-time diagnostics
            print(
                f"attempt {attempt}/{attempts} failed to download OSM graph: {exc}",
                file=sys.stderr,
                flush=True,
            )
            if attempt < attempts:
                time.sleep(min(30, 5 * attempt))
    if graph is None:
        print("ERROR: exhausted retries downloading OSM graph", file=sys.stderr, flush=True)
        return 1

    elapsed = time.perf_counter() - started
    nodes = graph.number_of_nodes()
    edges = graph.number_of_edges()
    print(
        f"Cached real road graph: {nodes} nodes, {edges} edges in {elapsed:.1f}s",
        flush=True,
    )
    if nodes < 1000:
        # The real central-Bengaluru drive network is tens of thousands of nodes;
        # anything tiny means we accidentally cached the demo grid.
        print(
            f"WARNING: graph has only {nodes} nodes - this does not look like the "
            "real network. Check ROAD_GRAPH_MODE and network access.",
            file=sys.stderr,
            flush=True,
        )
    if not target.exists():
        print(f"ERROR: expected cache file was not written: {target}", file=sys.stderr, flush=True)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
