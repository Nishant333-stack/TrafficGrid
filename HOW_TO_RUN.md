# How to Run — Bengaluru Event-Driven Traffic Forecasting

This system forecasts traffic impact for **planned** (rallies, festivals, sports,
construction) and **unplanned** (crashes, breakdowns) events, then recommends
manpower, barricading, and diversion plans. Backend = FastAPI + PostgreSQL + ML
(XGBoost/LightGBM) + OR-Tools; frontend = React/Vite served at `/app`.

There are two ways to run it: **Docker** (mirrors the Render deployment exactly)
or **local Python** (best for development). Both are described below, followed by
deploying to Render.

---

## What you need

- **PostgreSQL** (a plain managed/standalone Postgres — PostGIS is *not* required).
- **Python 3.11** (project target) and **Node 20+** (only to build the frontend
  for a local non-Docker run).
- The bundled dataset `Astram event data_anonymized - ...csv` (already in the repo).

The app reads configuration from environment variables (or a local `.env`):

| Variable | Purpose | Example |
|---|---|---|
| `DATABASE_URL` | Postgres connection (required) | `postgresql+psycopg2://user:pass@localhost:5432/trafficgrid` |
| `MODEL_DIR` | Trained model artifacts | `./models` (local) / `/app/models` (Docker) |
| `ROAD_GRAPH_MODE` | `demo` = offline grid (fast, fake roads); `live` = real OSM graph | `demo` (local) / `live` (Docker) |
| `ACTIVE_EVENT_INCLUDE_DEMO_FEEDS` | Blend synthetic "live" incidents | `true` |
| `PORT` | Server port (Render sets this) | `8000` |

---

## Option A — Docker (recommended, matches Render)

The image builds the React frontend, installs Python deps, bundles the trained
models, and on start runs: wait-for-DB → apply schema → load the CSV → seed →
serve. It is fully self-contained except for the database.

```bash
# 1. Build
docker build -t trafficgrid .

# 2. Start a Postgres (or point at any existing one)
docker network create tgnet
docker run -d --name tg-pg --network tgnet \
  -e POSTGRES_USER=user -e POSTGRES_PASSWORD=password -e POSTGRES_DB=trafficgrid \
  postgres:15

# 3. Run the app (it loads the CSV into the DB on first boot)
docker run --rm --name tg-app --network tgnet -p 8000:10000 \
  -e DATABASE_URL="postgresql+psycopg2://user:password@tg-pg:5432/trafficgrid" \
  -e ACTIVE_EVENT_INCLUDE_DEMO_FEEDS=true \
  trafficgrid
```

Open **http://localhost:8000/app**. First boot loads ~8,200 events and may take
30–60s; subsequent boots skip the load (it is idempotent).

---

## Option B — Local Python (development)

```bash
# 1. (optional) virtualenv
python3 -m venv .venv && source .venv/bin/activate

# 2. Install backend deps
pip install -r requirements.txt

# 3. Build the frontend bundle into dist/  (REQUIRED for the /app UI)
npm install
npm run build

# 4. Point at your Postgres
export DATABASE_URL="postgresql+psycopg2://user:password@localhost:5432/trafficgrid"
export MODEL_DIR="$(pwd)/models"
export ROAD_GRAPH_MODE=demo
export ACTIVE_EVENT_INCLUDE_DEMO_FEEDS=true

# 5. Initialize schema + load the historical CSV (idempotent)
python scripts/init_db.py        # creates tables (schema_render.sql)
python scripts/load_events.py    # loads the Astram CSV (~8,200 events)
python -m backend.data.seed_feedback --rows 40   # optional demo feedback

# 6. Run the API + UI
uvicorn main:app --host 127.0.0.1 --port 8000
```

> If your shell only has `python3`, use `python3` in place of `python` above.

Open **http://localhost:8000/app** (dashboard) or
**http://localhost:8000/docs** (interactive API).

> **Frontend note:** `dist/` is a build artifact. If the UI fails to load with a
> *"Expected a JavaScript module but got text/html"* MIME error, your `dist/` is
> stale — re-run `npm run build`. (Docker always builds it fresh.)

### Frontend dev server (hot reload, optional)
```bash
# Terminal 1: API
uvicorn main:app --port 8000
# Terminal 2: Vite dev server (proxies /events, /metrics, ... to :8000)
npm run dev    # http://127.0.0.1:5173/app/
```

---

## Deploy to Render

`render.yaml` is a Blueprint that provisions a free Postgres + a Docker web
service. From the Render dashboard: **New → Blueprint**, point at this repo, and
deploy. Key settings are already wired:

- `DATABASE_URL` is injected from the managed database.
- `MODEL_DIR=/app/models`, `ROAD_GRAPH_MODE=live`, `healthCheckPath=/platform/health`.
- The Docker build pre-downloads the real Bengaluru OSM drive graph
  (`scripts/build_graph_cache.py`) and bakes it into the image, so diversions
  route over actual streets with no runtime download. Set `ROAD_GRAPH_MODE=demo`
  for a fast offline grid (e.g. local dev without the cached graph).
- On boot the container waits for the DB, applies the schema, loads the CSV, and
  seeds demo feedback — so the dashboard has data on first deploy.

After deploy, the app is at `https://<service>.onrender.com/app`.

---

## Verifying it works

```bash
curl localhost:8000/platform/health        # -> {"status":"ok", ...}
curl localhost:8000/metrics/summary         # active/planned counts, accuracy
curl localhost:8000/events/active           # current unplanned incidents
curl localhost:8000/events/upcoming         # planned events (rallies/festivals/...)

# Forecast + deployment plan for one event:
curl -X POST localhost:8000/events/<event_id>/forecast -H 'X-User-Role: traffic_commander'
curl -X POST localhost:8000/events/<event_id>/plan     -H 'X-User-Role: traffic_commander'
```

A forecast returns `event_class` (`planned`/`unplanned`), severity, a duration
confidence interval, risk score, and personnel demand. A plan returns control
points, personnel/barricade allocations from nearby stations, and diversions.

`pytest tests/` runs the unit tests.

---

## Troubleshooting

| Symptom | Cause / Fix |
|---|---|
| Data endpoints 500 with `relation "events" does not exist` | Schema/data never loaded. Run `python scripts/init_db.py && python scripts/load_events.py`. On Docker/Render this happens automatically at startup. |
| `/platform/health` shows `degraded`, artifacts `false` | `MODEL_DIR` not pointing at the models. Set `MODEL_DIR=./models` (local) or `/app/models` (Docker). |
| UI blank, console: *module script … text/html* | Stale `dist/`. Run `npm run build`. |
| `DATABASE_URL ... is required` | Export `DATABASE_URL` (see table above). |
| Forecast 500, `pyarrow`/parquet error | `pip install -r requirements.txt` (pulls `pyarrow`). |
