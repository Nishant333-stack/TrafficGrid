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
    MODEL_DIR=/app/backend/ml/models \
    ROAD_GRAPH_MODE=demo \
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

RUN python -m backend.ml.bootstrap_models \
    && python -c "from backend.geo.road_graph import cache_demo_graph; cache_demo_graph()"

RUN chmod +x scripts/start.sh

EXPOSE 8000
CMD ["bash", "scripts/start.sh"]
