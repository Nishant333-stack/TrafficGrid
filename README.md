# Bengaluru Traffic Command — Event-Driven Congestion Forecasting

An event-driven system that **forecasts the traffic impact of incidents and planned events**, then **recommends an optimal response plan** — where to place officers, how many barricades, and which diversions to open — and **learns from field outcomes** to improve over time.

Built for the operational reality of Bengaluru traffic: political rallies, festivals, sports events, construction, crashes, and sudden gatherings that cascade into city-wide delays.

> **Status:** working MVP, deployable on Render. Forecasting, planning, real-road diversions, the field app, and the retrain loop are fully functional. Live weather is real; live incidents are real with an API key (fixtures otherwise). See [Real-time integrations](#real-time-integrations) for the honest live-vs-fixture breakdown.

---

## Table of contents

- [What it does (MVP capabilities)](#what-it-does-mvp-capabilities)
- [Problem statement](#problem-statement)
- [Architecture](#architecture)
- [Data & request flow](#data--request-flow)
- [Tech stack](#tech-stack)
- [Project structure](#project-structure)
- [Running locally](#running-locally)
- [Deployment](#deployment)
- [API reference](#api-reference)
- [Machine learning](#machine-learning)
- [Real-time integrations](#real-time-integrations)
- [Post-event learning loop](#post-event-learning-loop)
- [Design principles](#design-principles)
- [Limitations & roadmap](#limitations--roadmap)
- [Documentation map](#documentation-map)

---

## What it does (MVP capabilities)

These are the proven, end-to-end capabilities — each is wired from data → model/optimizer → API → UI.

| # | MVP | What it delivers |
|---|-----|------------------|
| 1 | **Incident impact forecasting** | Severity (HIGH/LOW), duration confidence interval (Q25/Q50/Q75 with survival adjustment), corridor risk score, and derived operational metrics (expected delay, queue length, personnel demand). |
| 2 | **Automated deployment planning** | Discovers optimal control points on the real road network, sizes manpower + barricades by severity, and allocates them from the nearest police stations via OR-Tools (greedy fallback). |
| 3 | **Real-road diversions** | Routes around the blocked corridor over the actual OpenStreetMap network (A\* with congestion/risk weighting), drawn along true road geometry. |
| 4 | **Command console** | Full-width live map (Leaflet), event search by name/road, per-event recommendations, executive ROI strip, and a live WebSocket feed. |
| 5 | **Field operations app** | Officer login, assigned control points, and one-tap status reporting (acknowledge, GPS check-in, need backup/barricades, road cleared) back to the command center. |
| 6 | **Post-event learning loop** | Field feedback (actual clearance times) becomes corrected training labels; retrain rebuilds the models and hot-reloads them with no restart. |
| 7 | **Live operational context** | Real per-event weather (rainfall/wind) feeds the delay model; live traffic incidents via a keyed provider, with graceful fixture fallback. |

---

## Problem statement

> *How can historical and real-time data be used to forecast event-related traffic impact and recommend optimal manpower, barricading, and diversion plans?*

Today, incident response is **experience-driven**: impact isn't quantified in advance, resource deployment relies on intuition, and there's no systematic post-event learning. This system turns each incident into a quantified forecast + an executable, optimized plan, and closes the loop with field feedback.

---

## Architecture

```
                          ┌─────────────────────────────────────────────┐
                          │  React + Leaflet SPA  (/app, /app/field)     │
                          │  console · search · recommendations · field  │
                          └───────────────┬──────────────────────────────┘
                                          │ REST + WebSocket (same origin)
                          ┌───────────────▼──────────────────────────────┐
                          │             FastAPI (gunicorn/uvicorn)         │
                          │  RBAC · caching · graceful degradation         │
                          └──┬───────────┬───────────┬───────────┬────────┘
                             │           │           │           │
                   ┌─────────▼──┐  ┌─────▼─────┐ ┌───▼──────┐ ┌──▼─────────────┐
                   │ predict.py │  │generate_  │ │allocation│ │ integrations    │
                   │ severity / │  │plan.py    │ │ OR-Tools │ │ weather (live)  │
                   │ duration / │  │ control   │ │ + greedy │ │ incidents (key) │
                   │ risk       │  │ points    │ │          │ │ speeds/sensors  │
                   └─────┬──────┘  └────┬──────┘ └──────────┘ └─────────────────┘
                         │              │
                   ┌─────▼──────────────▼─────┐     ┌──────────────────────────┐
                   │ ML artifacts (MODEL_DIR) │     │ OSM road graph (OSMnx /   │
                   │ *.pkl + risk_density.pq  │     │ NetworkX, cached/baked)   │
                   └──────────────────────────┘     └──────────────────────────┘
                         │
                   ┌─────▼───────────────────────────────────────────────────┐
                   │ PostgreSQL (+PostGIS): events, police_stations, feedback,│
                   │ plan_workflows, audit_log, field_status_updates          │
                   └──────────────────────────────────────────────────────────┘
```

A more detailed diagram lives in [`ARCHITECTURE.md`](ARCHITECTURE.md) / [`ARCHITECTURE.mmd`](ARCHITECTURE.mmd).

---

## Data & request flow

```
CSV (Astram export)
   └─ load_events.py → normalize, dedupe, infer zones, clean durations → PostgreSQL
                                                                              │
POST /events/{id}/forecast ── predict.py ── severity + duration + risk + ops metrics
                                                                              │
POST /events/{id}/plan ── generate_plan.py ── control points (OSM) ── allocation (OR-Tools)
                                            └─ diversion.py ── real-road A* routes
                                                                              │
POST /events/{id}/feedback ── workflow.py ── feedback table / JSONL
                                                                              │
POST /models/retrain ── train_models.py ── feedback-corrected labels → new artifacts → hot reload
```

---

## Tech stack

| Layer | Technology |
|-------|-----------|
| API | FastAPI (v0.4.0), Gunicorn + Uvicorn workers, WebSockets |
| ML | XGBoost / LightGBM, scikit-learn (quantile regression, classification) |
| Optimization | Google OR-Tools (CBC), with deterministic greedy fallback |
| Geospatial | OSMnx + NetworkX (road graphs, A\* routing), PostGIS, haversine utils |
| Data | PostgreSQL 15, SQLAlchemy 2, pandas |
| Frontend | React 18, Vite, Leaflet, vanilla CSS |
| Live data | Open-Meteo (weather, keyless); pluggable live-incident provider (fixtures by default) |
| Deploy | Docker (multi-stage), Render Blueprint (`render.yaml`) |

---

## Project structure

```
backend/
  api/main.py            FastAPI app: endpoints, RBAC, caching, WebSocket, graceful degradation
  ml/
    predict.py           Forecasting engine (severity, duration, risk, ops metrics)
    train_models.py      Training pipeline (+ feedback-corrected labels) → MODEL_DIR
    feature_cleaning.py  Categorical normalization, duration caps
    model_monitoring.py  Drift detection, backtests, retrain plan
  optimization/
    generate_plan.py     End-to-end deployment plan orchestration
    allocation.py        Personnel/barricade allocation (OR-Tools + greedy)
    control_points.py    Control-point discovery on the road graph
    diversion.py         Real-road diversion routing (A*, congestion-weighted)
    resource_sizing.py   Severity → resource counts
    multi_incident.py    Concurrent multi-incident coordination
  geo/road_graph.py      OSM graph load/cache/bake + demo-grid fallback
  integrations/          Live feeds: weather (live), incidents (keyed), speeds/sensors
  data/                  load_data, workflow (plans/audit), seed_feedback
  monitoring/            ROI metrics, operational metrics, platform health
  config/                env loading, DB engine
scripts/
  start.sh               Render entrypoint: wait-for-db → schema → load → seed → gunicorn
  wait_for_db.py         Block until Postgres accepts connections
  init_db.py             Apply schema
  load_events.py         Idempotent CSV → DB loader
  build_graph_cache.py   Pre-download the real OSM graph at build time (bounded, best-effort)
  retrain.py             CLI: retrain from events + feedback, reload cache
src/                     React SPA (App.jsx console + field view, styles.css)
models/                  Trained artifacts (.pkl, risk_density.parquet, survival table)
schema.sql               Postgres + PostGIS schema (schema_render.sql for managed PG)
Dockerfile               Multi-stage build (frontend → python runtime)
render.yaml              Render Blueprint (free Postgres + Docker web service)
```

---

## Running locally

Quickest path (single process, serves API + built UI on the same origin):

```bash
pip install -r requirements.txt
npm ci && npm run build                       # build the frontend into dist/
export DATABASE_URL="postgresql+psycopg2://user:pass@localhost:5432/trafficgrid"
export MODEL_DIR="$(pwd)/models" ROAD_GRAPH_MODE=demo
python scripts/init_db.py && python scripts/load_events.py   # one-time data load
uvicorn main:app --port 8000
```

Open **http://localhost:8000/app** (console) and **http://localhost:8000/app/field** (field view).

For the hot-reload frontend dev server (Vite on :5173, proxies API/WS to :8000) and offline/no-DB modes, see **[HOW_TO_RUN.md](HOW_TO_RUN.md)**.

---

## Deployment

One-click via the Render Blueprint — provisions a free PostgreSQL + a Docker web service:

1. Render → **New → Blueprint** → connect this repo → **Apply**.
2. App lands at `https://<service>.onrender.com/app`.

The deploy is hardened to **never hang or 500** during bring-up: the real-graph build is time-bounded and best-effort (falls back to the demo grid), the DB init is idempotent and waited-on, and the app degrades to live feeds + zeroed metrics if the database isn't reachable yet.

Full guide, env-var reference, and the live-vs-demo road-graph trade-off: **[DEPLOY_RENDER.md](DEPLOY_RENDER.md)**.

---

## API reference

Key endpoints (full interactive docs at `/docs`):

| Method | Path | Purpose |
|--------|------|---------|
| `POST` | `/events/{id}/forecast` | Severity, duration CI, risk, operational metrics |
| `POST` | `/events/{id}/plan` | Control points, allocations, diversions, shortfalls |
| `POST` | `/events/{id}/feedback` | Record actual duration / officer rating / adjustments |
| `GET`  | `/events/active`, `/events/upcoming` | Live + planned events |
| `GET`  | `/metrics/summary`, `/metrics/roi`, `/metrics/operational` | Dashboards |
| `POST` | `/models/retrain` *(admin)* | Retrain from feedback; `GET /models/retrain/status` |
| `GET`  | `/models/drift`, `/models/retrain-plan` | Drift detection & recommendation |
| `GET`  | `/field/assignments`, `POST /field/status` | Field officer flow |
| `POST` | `/workflow/plans`, `/workflow/plans/{id}/approval` | Plan lifecycle + audit |
| `WS`   | `/ws/live` | 5s metrics + newly-active-events broadcast |

Multi-tenancy and RBAC are header-driven (`X-Tenant-Id`, `X-User-Id`, `X-User-Role`).

---

## Machine learning

Artifacts live in `MODEL_DIR` and are loaded (LRU-cached) by `predict.py`:

- **Severity classifier** — probability of a HIGH-impact incident → thresholded label.
- **Duration quantile regressors** — Q25/Q50/Q75 minutes, capped per event type, adjusted for right-censoring via a **survival table**.
- **Risk density** (`risk_density.parquet`) — corridor × hour-bucket × day-of-week → risk score, with graceful fallbacks (corridor avg → global avg).
- **Operational metrics** — convert point estimates into actionable numbers (expected delay, queue length in metres, personnel demand, confidence level), fused with live context (speeds, sensor counts, weather).

Training (`train_models.py`) reads events from Postgres (or a CSV fallback), **merges officer-reported actual durations from feedback as corrected labels**, and writes fresh artifacts. See [Post-event learning loop](#post-event-learning-loop).

---

## Real-time integrations

Honest status of each feed (`backend/integrations/integrations.py`). All degrade gracefully to fixtures so the app never breaks.

| Feed | Source | Status |
|------|--------|--------|
| **Weather** | Open-Meteo | **Opt-in** (`LIVE_WEATHER=true`), keyless, fetched **per incident location**; fixtures by default |
| **Traffic incidents** | Pluggable provider | Realistic fixtures by default; the integrations layer exposes a configurable **live-incident API-key slot** to plug in a provider |
| GPS corridor speeds | — | Fixture (real source requires a paid API) |
| CCTV / ANPR counts | — | Fixture (real source requires integration) |

Weather flows into the forecast's delay/flood factors; incidents appear as live map markers and active events.

---

## Post-event learning loop

The loop is closed and demonstrable:

```
Officer reports actual clearance time   →  POST /events/{id}/feedback
        │
        ▼
Retrain merges actuals as corrected labels  →  scripts/retrain.py  or  POST /models/retrain
        │
        ▼
New artifacts written to MODEL_DIR  →  predict.reload_artifacts() hot-swaps the cache
        │
        ▼
Subsequent forecasts use the improved model — no restart
```

Retraining runs in the background and is fast on the current dataset. Drift can be inspected via `/models/drift`.

---

## Design principles

- **Graceful degradation everywhere.** No DB → live feeds + zeroed metrics. No road graph → demo grid. No OR-Tools → greedy allocation. No live feed → fixtures. The app stays up.
- **Separation of concerns.** ML (`predict`) ≠ geography/optimization (`generate_plan`, `allocation`, `diversion`) ≠ API (`main`). Each is testable in isolation.
- **Idempotent, observable startup.** `start.sh` waits for the DB, applies schema, loads data, seeds, and prints diagnostics — safe to re-run.
- **Signature-based caching.** Forecasts/plans are cached by event signature with TTLs to avoid recomputing for rapidly-polled events.
- **Auditability.** Plan lifecycle transitions and sensitive actions are written to an append-only audit log.

---

## Limitations & roadmap

Known gaps (called out honestly):

- **AuthN/AuthZ is header-based** (`X-User-Role`) — fine for a pilot, not production; needs real auth before public deployment.
- **Some inputs are synthetic** — police-station inventory is deterministically seeded; GPS speeds and sensor counts are fixtures.
- **Operational coefficients are estimates** (e.g., affected-users-per-minute) — directional, not yet calibrated against ground truth.
- **Labeled data is modest** — duration labels and feedback rows are limited; the retrain loop improves this over time.
- **Free-tier deploy trade-offs** — the real OSM graph build can be slow; the app falls back to the demo grid to keep deploys reliable.

Roadmap: real authentication, calibrated impact coefficients, live GPS/sensor feeds, and a separate worker for retraining at scale.

---

## Documentation map

| Doc | Contents |
|-----|----------|
| [HOW_TO_RUN.md](HOW_TO_RUN.md) | Local dev (Docker & native), env vars, offline mode, retrain |
| [DEPLOY_RENDER.md](DEPLOY_RENDER.md) | Render Blueprint deploy, env reference, troubleshooting |
| [ARCHITECTURE.md](ARCHITECTURE.md) | Detailed architecture & diagrams |
| [CLAUDE.md](CLAUDE.md) | Deep module-by-module reference for the data/ML layer |
| `/docs` (runtime) | Interactive OpenAPI explorer |

---

*Astram is the Bengaluru traffic incident reporting system used as the historical data source. This project is an MVP/pilot, not an official deployment.*
