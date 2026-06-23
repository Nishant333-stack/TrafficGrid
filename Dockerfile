# syntax=docker/dockerfile:1

FROM node:20-slim AS frontend
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci
COPY index.html vite.config.js ./
COPY src ./src
ENV VITE_API_BASE=
RUN npm run build

FROM python:3.11-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    MPLCONFIGDIR=/tmp/matplotlib \
    MODEL_DIR=/app/models \
    ROAD_GRAPH_MODE=live \
    WEB_CONCURRENCY=1

RUN apt-get update && apt-get install -y --no-install-recommends \
    bash \
    libgomp1 \
    libgeos-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
COPY --from=frontend /app/dist ./dist

# Essential artifacts (must succeed): trained models + offline demo graph.
RUN python -m backend.ml.bootstrap_models \
    && python -c "from backend.geo.road_graph import cache_demo_graph; cache_demo_graph()"

# Best-effort: pre-download the real Bengaluru OSM graph so diversions route
# over real streets. Hard time-bounded AND non-fatal -- a slow/large download or
# Overpass stall can neither hang nor fail the build; the app still deploys and
# falls back to the demo grid at runtime. Tune the cap with GRAPH_BUILD_TIMEOUT;
# force a fast demo-only build with ROAD_GRAPH_MODE=demo.
ENV GRAPH_BUILD_TIMEOUT=420
RUN timeout "${GRAPH_BUILD_TIMEOUT}" python scripts/build_graph_cache.py \
    || echo "WARN: real road graph not baked (timed out/failed); runtime uses the demo grid"

RUN chmod +x scripts/start.sh

EXPOSE 10000
CMD ["bash", "scripts/start.sh"]
