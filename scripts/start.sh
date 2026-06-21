#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."
export PYTHONPATH="$(pwd):${PYTHONPATH:-}"

echo "=== STARTUP DIAGNOSTICS ==="
echo "PWD: $(pwd)"
echo "PORT: ${PORT:-not set}"
echo "DATABASE_URL set: $(if [ -n "${DATABASE_URL:-}" ]; then echo YES; else echo NO; fi)"
echo "ROAD_GRAPH_MODE: ${ROAD_GRAPH_MODE:-not set}"
echo "MODEL_DIR: ${MODEL_DIR:-not set}"
echo "Contents of /app/dist: $(ls /app/dist 2>/dev/null || echo 'NOT FOUND')"
echo "==========================="

echo "Initializing database..."
python scripts/init_db.py || echo "WARNING: Database initialization failed."

echo "Seeding feedback data..."
python -m backend.data.seed_feedback --rows 40 || echo "Feedback seeding skipped."

python -m backend.ml.bootstrap_models
python -c "from backend.geo.road_graph import cache_demo_graph; cache_demo_graph()"

echo "Starting gunicorn on port ${PORT:-10000}..."
exec gunicorn main:app \
  --worker-class uvicorn.workers.UvicornWorker \
  --bind "0.0.0.0:${PORT:-10000}" \
  --workers "${WEB_CONCURRENCY:-1}" \
  --timeout "${GUNICORN_TIMEOUT:-120}" \
  --graceful-timeout 30 \
  --keep-alive 5
