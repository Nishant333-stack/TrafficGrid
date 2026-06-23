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

if [ -n "${DATABASE_URL:-}" ]; then
  # Render's free Postgres can take a while to accept connections on a cold
  # deploy. Wait for it before touching the schema so init never silently
  # no-ops and leaves the app serving 500s against missing tables.
  echo "Waiting for database to accept connections..."
  python scripts/wait_for_db.py --timeout 120 || echo "WARNING: database not ready; continuing."

  echo "Initializing database schema..."
  python scripts/init_db.py || echo "WARNING: schema init failed; app will retry on startup."

  echo "Loading Astram event data (idempotent; skips if already populated)..."
  python scripts/load_events.py || echo "WARNING: event data load skipped."

  echo "Seeding feedback data (skipped if already present)..."
  python -m backend.data.seed_feedback --rows 40 --skip-if-present || echo "Feedback seeding skipped."
else
  echo "DATABASE_URL not set; skipping database initialization."
fi

python -m backend.ml.bootstrap_models
python -c "from backend.geo.road_graph import cache_demo_graph; cache_demo_graph()"

echo "Starting gunicorn on port ${PORT:-10000}..."
exec gunicorn main:app \
  --worker-class uvicorn.workers.UvicornWorker \
  --bind "0.0.0.0:${PORT:-10000}" \
  --workers "${WEB_CONCURRENCY:-1}" \
  --timeout "${GUNICORN_TIMEOUT:-120}" \
  --graceful-timeout "${GUNICORN_GRACEFUL_TIMEOUT:-10}" \
  --keep-alive 5
