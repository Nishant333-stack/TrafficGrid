# Bengaluru Traffic Command — System Architecture

**An event-driven traffic congestion forecasting and resource-optimization platform.**

When an incident or planned event occurs (a crash, a breakdown, a rally, a festival), the system forecasts its traffic impact, generates an executable response plan — control points, manpower, barricades, and real-road diversions — and learns from field outcomes to sharpen future predictions. It targets the operational reality of Bengaluru traffic, where localized events cascade into city-wide congestion and response today is largely experience-driven.

This document is the authoritative technical reference: system context, component design, data model, request lifecycles, the ML and learning pipelines, deployment topology, and the resilience/security posture. Diagrams are Mermaid and render on GitHub.

---

## Table of contents

1. [Design goals & principles](#1-design-goals--principles)
2. [System context](#2-system-context)
3. [Container & component architecture](#3-container--component-architecture)
4. [Component responsibilities](#4-component-responsibilities)
5. [Data model](#5-data-model)
6. [Request lifecycles](#6-request-lifecycles)
7. [Machine learning pipeline](#7-machine-learning-pipeline)
8. [Post-event learning loop](#8-post-event-learning-loop)
9. [Plan lifecycle (state machine)](#9-plan-lifecycle-state-machine)
10. [Real-time integrations](#10-real-time-integrations)
11. [Resilience & graceful degradation](#11-resilience--graceful-degradation)
12. [Deployment architecture](#12-deployment-architecture)
13. [Caching strategy](#13-caching-strategy)
14. [Security & multi-tenancy](#14-security--multi-tenancy)
15. [Observability & operations](#15-observability--operations)
16. [Performance characteristics](#16-performance-characteristics)
17. [Technology stack](#17-technology-stack)
18. [Limitations & roadmap](#18-limitations--roadmap)

---

## 1. Design goals & principles

| Principle | How it shows up |
|-----------|-----------------|
| **Separation of concerns** | ML (`predict`) ≠ geospatial/optimization (`generate_plan`, `allocation`, `diversion`) ≠ transport/API (`main`). Each is independently testable. |
| **Graceful degradation** | Every external dependency has a fallback: no DB → live feeds + seed; no road graph → demo grid; no OR-Tools → greedy; no live feed → fixtures. The service never hard-fails on a missing dependency. |
| **Idempotent, observable startup** | The boot sequence waits for the DB, applies schema, loads data, and seeds — safe to re-run, with diagnostics printed. |
| **Determinism where it matters** | Zone inference, station seeding, and the demo road graph are deterministic so dev/CI is reproducible without network. |
| **Closed feedback loop** | Field outcomes become corrected training labels; retraining measurably moves predictions. |
| **Auditability** | Plan transitions and sensitive actions are appended to an immutable audit log. |

---

## 2. System context

```mermaid
flowchart TB
    officer["Field officer<br/>(mobile / field view)"]
    commander["Traffic commander<br/>(command console)"]

    subgraph platform["Bengaluru Traffic Command"]
        api["FastAPI service<br/>API + WebSocket + static SPA"]
    end

    osm["OpenStreetMap<br/>(OSMnx / Overpass)"]
    weather["Open-Meteo<br/>(live weather, keyless)"]
    incidents["Live incident provider<br/>(optional, pluggable key slot)"]
    astram["Astram CSV export<br/>(historical incidents)"]

    commander -->|"REST + WS"| api
    officer -->|"assignments / status"| api
    astram -->|"bulk load"| api
    api -->|"road graph"| osm
    api -->|"per-event weather"| weather
    api -->|"active incidents"| incidents

    api -.->|"forecasts, plans,<br/>diversions"| commander
    api -.->|"control-point tasks"| officer
```

The platform is a single deployable unit (API + bundled SPA, same origin) backed by PostgreSQL, with optional external feeds that all degrade to fixtures.

---

## 3. Container & component architecture

```mermaid
flowchart TB
    subgraph client["Client (React SPA, served at /app)"]
        console["Command console<br/>map · search · recommendations · ROI"]
        field["Field view<br/>login · assignments · status"]
    end

    subgraph svc["FastAPI service (gunicorn + uvicorn workers)"]
        direction TB
        apilayer["API layer (main.py)<br/>routing · RBAC · caching · WebSocket<br/>graceful degradation"]

        subgraph ml["ML / forecasting"]
            predict["predict.py<br/>severity · duration · risk · ops metrics"]
            train["train_models.py<br/>training + feedback labels"]
            monitor["model_monitoring.py<br/>drift · backtest · retrain plan"]
        end

        subgraph opt["Geospatial & optimization"]
            plan["generate_plan.py"]
            cps["control_points.py"]
            alloc["allocation.py (OR-Tools + greedy)"]
            div["diversion.py (A* real-road)"]
            sizing["resource_sizing.py"]
            multi["multi_incident.py"]
        end

        subgraph data["Data & domain"]
            workflow["workflow.py<br/>plan lifecycle + audit"]
            roi["roi_metrics.py / operational_monitoring.py"]
            integ["integrations.py<br/>weather · incidents · speeds · sensors"]
            loader["load_data.py / scripts"]
        end
    end

    roadgraph["OSM road graph<br/>road_graph.py (cache/bake/demo)"]
    artifacts["ML artifacts (MODEL_DIR)<br/>*.pkl + parquet"]
    pg[("PostgreSQL<br/>events · stations · feedback<br/>plan_workflows · audit · field_status")]

    console -->|REST/WS| apilayer
    field -->|REST| apilayer
    apilayer --> predict
    apilayer --> plan
    apilayer --> workflow
    apilayer --> roi
    apilayer --> integ
    predict --> artifacts
    plan --> cps --> roadgraph
    plan --> alloc
    plan --> div --> roadgraph
    plan --> sizing
    apilayer --> pg
    workflow --> pg
    train --> artifacts
    train --> pg
    integ --> weather2["external feeds"]
```

---

## 4. Component responsibilities

| Module | Responsibility | Key fallback |
|--------|----------------|--------------|
| `backend/api/main.py` | HTTP/WS endpoints, RBAC, request context, signature caching, response shaping, **degradation guards** | DB down → live feeds + zeroed metrics |
| `backend/ml/predict.py` | Forecast engine: severity prob, duration quantiles (Q25/50/75) + survival adjustment, risk score, operational metrics | Missing artifacts → `FileNotFoundError` (signals retrain) |
| `backend/ml/train_models.py` | Training pipeline; merges officer feedback as corrected labels; writes artifacts | CSV fallback when no DB |
| `backend/ml/model_monitoring.py` | Drift detection, backtests, retrain recommendation | — |
| `backend/optimization/generate_plan.py` | Orchestrates a deployment plan end-to-end | warnings on partial failure |
| `backend/optimization/control_points.py` | Finds control points on the road graph (arterial-weighted) | demo graph |
| `backend/optimization/allocation.py` | Personnel/barricade allocation | OR-Tools → greedy → subprocess-isolated |
| `backend/optimization/diversion.py` | Real-road A* diversions, congestion/risk weighted, true geometry | advisory routes / demo grid |
| `backend/optimization/resource_sizing.py` | Severity → resource counts | — |
| `backend/optimization/multi_incident.py` | Concurrent multi-incident coordination | — |
| `backend/geo/road_graph.py` | OSM graph load/cache/bake; **demo-grid fallback** | runtime demo fallback (no request-time download) |
| `backend/integrations/integrations.py` | Live feeds: weather (live), incidents (keyed), speeds/sensors (fixtures) | per-feed fixtures |
| `backend/data/workflow.py` | Plan versioning, approval chain, audit log | JSONL when no DB |
| `backend/data/load_data.py` | CSV normalize/dedupe/zone-infer/**duration cleaning** | — |
| `backend/monitoring/*` | ROI, operational metrics, platform health, retention | safe defaults |
| `backend/config/{db,env_loader}.py` | Engine factory (lazy, pool pre-ping), env loading | — |

---

## 5. Data model

PostgreSQL is the system of record. PostGIS is **not** required (Render-managed Postgres lacks it); geospatial math is done in Python (`geo_utils.haversine`). Multi-tenancy is carried by `tenant_id` columns.

```mermaid
erDiagram
    tenants ||--o{ app_users : has
    events ||--o{ feedback : "receives"
    events ||--o{ field_status_updates : "tracked by"
    events ||--o{ plan_workflows : "planned for"

    tenants {
        text id PK
        text name
        text region
        text environment
    }
    app_users {
        text id PK
        text tenant_id FK
        text display_name
        text role
        text police_station
        bool active
    }
    events {
        text id PK
        text event_type "planned | unplanned"
        float latitude
        float longitude
        text address
        text event_cause
        bool requires_road_closure
        timestamptz start_datetime
        text status
        text corridor
        text zone
        text police_station
        int duration_minutes
    }
    police_stations {
        serial id PK
        text name UK
        text zone
        float latitude
        float longitude
        int available_personnel
        int available_barricades
    }
    feedback {
        bigserial id PK
        text event_id FK
        text predicted_severity
        int predicted_duration_minutes
        int actual_duration_minutes
        int officer_rating "1..5"
        bool plan_accepted
        int adjusted_personnel
        jsonb plan_json
        timestamptz created_at
    }
    plan_workflows {
        bigserial id PK
        text plan_id
        text event_id
        int version
        text status
        text tenant_id
        jsonb approval_chain
        jsonb plan_json
    }
    audit_log {
        bigserial id PK
        text audit_id UK
        text tenant_id
        text actor
        text action
        text resource_type
        jsonb details
    }
    field_status_updates {
        bigserial id PK
        text status_id UK
        text event_id
        text station
        text control_point_node_id
        text status
        float latitude
        float longitude
    }
```

**Data quality:** the loader nulls implausible durations (negative, or > 24 h — administrative "ticket left open" artifacts) so they act as censored observations rather than skewing statistics. This dropped the dataset's mean duration from ~6,234 min to ~99 min.

---

## 6. Request lifecycles

### 6.1 Forecast

```mermaid
sequenceDiagram
    autonumber
    participant UI as Console
    participant API as main.py
    participant Cache as Forecast cache (TTL 300s)
    participant P as predict.py
    participant A as Artifacts (MODEL_DIR)
    participant I as integrations.py

    UI->>API: POST /events/{id}/forecast
    API->>API: resolve event (DB → integrations → seed)
    API->>Cache: signature lookup
    alt cache hit
        Cache-->>API: cached forecast
    else miss
        API->>P: predict_impact(event)
        P->>A: load_artifacts() [lru-cached]
        P->>I: operational_context_for_event() (live weather)
        P->>P: severity · Q25/50/75 + survival · risk · ops metrics
        P-->>API: forecast
        API->>Cache: store
    end
    API-->>UI: forecast JSON
```

### 6.2 Deployment plan

```mermaid
sequenceDiagram
    autonumber
    participant UI as Console
    participant API as main.py
    participant G as generate_plan.py
    participant RG as road_graph.py
    participant CP as control_points.py
    participant AL as allocation.py
    participant DV as diversion.py

    UI->>API: POST /events/{id}/plan
    API->>G: generate_deployment_plan(event + prediction)
    G->>RG: get_graph(point) [real OSM or demo fallback]
    G->>CP: find_control_points(radius by severity)
    G->>AL: allocate_personnel / barricades
    Note over AL: OR-Tools CBC in subprocess<br/>→ greedy fallback on failure
    G->>DV: compute_diversions (A*, congestion-weighted)
    DV-->>G: real-road routes (true geometry)
    G-->>API: control points, allocations, diversions, shortfalls, warnings
    API-->>UI: plan JSON
```

### 6.3 Feedback + live broadcast

```mermaid
sequenceDiagram
    autonumber
    participant UI as Console / Field
    participant API as main.py
    participant DB as PostgreSQL
    participant WS as /ws/live (5s loop)

    UI->>API: POST /events/{id}/feedback {accepted, actual_duration, adjusted_personnel, rating}
    API->>DB: insert feedback (+ audit_log)
    API-->>UI: {stored: true}
    loop every 5s
        WS->>API: active_events() + metrics_summary()
        API-->>WS: metrics + newly_active_events
        WS-->>UI: push
    end
```

---

## 7. Machine learning pipeline

```mermaid
flowchart LR
    subgraph training["Training (offline / on-demand)"]
        src["events (DB or CSV)"] --> clean["clean + derive features<br/>feature_cleaning.py"]
        fb["feedback actuals"] -->|"corrected labels"| clean
        clean --> sev["severity classifier"]
        clean --> dur["duration quantile regressors<br/>Q25 / Q50 / Q75"]
        clean --> risk["risk density<br/>corridor × hour × dow"]
        clean --> surv["survival table<br/>censoring adjustment"]
        sev & dur & risk & surv --> art[("MODEL_DIR artifacts")]
    end

    subgraph inference["Inference (per request)"]
        ev["event features"] --> ff["build_feature_frame"]
        ff --> load["load_artifacts (lru cache)"]
        load --> out["severity label + prob<br/>duration CI (Q25/50/75 + survival)<br/>risk score"]
        ctx["live context:<br/>weather · speeds · sensors"] --> ops["operational metrics:<br/>expected delay · queue · personnel demand · confidence"]
        out --> ops
    end

    art -. reload_artifacts() .-> load
```

**Models:** XGBoost/LightGBM via scikit-learn pipelines. Duration is modeled as **quantile regression** (not a point estimate) to express uncertainty, then corrected for right-censoring (incidents still open at data cutoff bias the median low). Risk density is a lookup table with corridor → corridor-average → global-average fallbacks.

---

## 8. Post-event learning loop

The loop is closed and **empirically verified to move predictions**: in a controlled test, injecting officer feedback (actual = 300 min) for a corridor shifted its predicted median from **35 min → 277 min** after retraining.

```mermaid
flowchart LR
    A["Officer reports actual outcome<br/>POST /events/id/feedback"] --> B["feedback table"]
    B --> C["apply_feedback_labels()<br/>actuals override duration labels"]
    C --> D["run_training()<br/>rebuild artifacts in MODEL_DIR"]
    D --> E["predict.reload_artifacts()<br/>hot-swap lru cache"]
    E --> F["subsequent forecasts use<br/>improved model (no restart)"]
    F -.->|"next incident"| A

    T1["scripts/retrain.py (CLI)"] --> D
    T2["POST /models/retrain (admin, background)"] --> D
    M["model_monitoring: /models/drift<br/>/models/retrain-plan"] -.->|recommends| T2
```

Retraining runs in a background thread on the API (fast on the current dataset) or via the CLI. Drift can be inspected before deciding to retrain.

---

## 9. Plan lifecycle (state machine)

```mermaid
stateDiagram-v2
    [*] --> draft
    draft --> submitted: submit
    submitted --> approved: traffic_commander + zone_superintendent approve
    submitted --> draft: revise
    approved --> activated: deploy
    activated --> closed: incident cleared
    closed --> [*]

    note right of submitted
        Each transition increments version,
        updates approval_chain, and writes
        an immutable audit_log entry.
    end note
```

---

## 10. Real-time integrations

All feeds live behind `integrations.py` and degrade to fixtures, so the app never breaks on a missing feed or key.

```mermaid
flowchart TB
    subgraph feeds["integrations.py"]
        w["weather_for_point(lat,lon)"]
        i["live_incidents()"]
        s["gps_speed_feed / sensor_counts"]
    end
    w -->|"opt-in (LIVE_WEATHER), keyless"| om["Open-Meteo<br/>per-event rainfall/wind"]
    w -.->|"default / on failure"| wf["fixture"]
    i -->|"if live-incident key configured"| live["live incident provider"]
    i -.->|"default / failure"| inf["fixtures"]
    s -.-> sf["fixtures (paid sources)"]
```

| Feed | Source | Status |
|------|--------|--------|
| Weather | Open-Meteo | **Opt-in** (`LIVE_WEATHER=true`), keyless, per incident location → drives delay/flood factors; fixtures by default |
| Incidents | Pluggable provider | Realistic fixtures by default; integrations layer exposes a configurable live-incident API-key slot |
| GPS speeds | — | Fixture (real source needs a paid API) |
| CCTV/ANPR counts | — | Fixture (needs integration) |

> Live external fetches are opt-in so the deployed server makes no blocking
> network calls in request paths (a key reason for prior health-check timeouts).

Live values are TTL-cached per coordinate and fetched with verified TLS (certifi) and short timeouts so they never block the request path.

---

## 11. Resilience & graceful degradation

```mermaid
flowchart TD
    req["Incoming request"] --> db{"DB reachable?"}
    db -->|yes| real["Real data path"]
    db -->|no| deg["Degrade:<br/>active_events → live feeds<br/>metrics → zeros<br/>planned/roi → seed file"]
    real --> rg{"Road graph baked?"}
    deg --> rg
    rg -->|yes| osm["Route on real OSM"]
    rg -->|no| demo["Demo grid (instant)"]
    osm --> solver{"OR-Tools available?"}
    demo --> solver
    solver -->|yes| cbc["CBC optimal allocation"]
    solver -->|no| greedy["Greedy allocation"]
    cbc --> resp["Response (never 500 on missing dep)"]
    greedy --> resp
```

Each fallback is independently exercised; the WebSocket loop also catches per-tick errors so a transient backend hiccup never drops the live connection.

---

## 12. Deployment architecture

Single Docker image (multi-stage: frontend build → Python runtime), deployed via a **Render Blueprint** that provisions a free Postgres + a web service.

```mermaid
flowchart TB
    subgraph build["Docker build (multi-stage)"]
        fe["Stage 1: node — npm run build → dist/"]
        be["Stage 2: python — pip install, copy app + dist"]
        bm["bake models + demo graph (must succeed)"]
        bg["bake real OSM graph (best-effort, time-bounded)"]
        fe --> be --> bm --> bg
    end

    subgraph render["Render"]
        web["Web service (Docker)<br/>gunicorn + uvicorn worker<br/>$PORT, health=/platform/health"]
        pgr[("Managed PostgreSQL")]
        web -->|DATABASE_URL| pgr
    end

    bg --> web

    subgraph boot["start.sh (entrypoint, idempotent)"]
        s1["wait_for_db"] --> s2["init_db (schema)"] --> s3["load_events (CSV)"] --> s4["seed_feedback"] --> s5["bootstrap models + demo graph"] --> s6["gunicorn"]
    end
    web -.runs.-> boot
```

**Build robustness:** the real-graph download is time-bounded (`timeout`) and non-fatal — it can neither hang nor fail the build. If it doesn't complete, runtime falls back to the demo grid. This prevents the classic failure where a hung build leaves a stale container serving a pre-`DATABASE_URL` config.

---

## 13. Caching strategy

| Cache | Scope | TTL | Key |
|-------|-------|-----|-----|
| Forecast | in-process dict | 300 s | event signature (id, status, cause, corridor, …) |
| Plan | in-process dict | 600 s | event signature |
| Model artifacts | `lru_cache(maxsize=1)` | until `reload_artifacts()` | MODEL_DIR |
| Weather | per-coordinate dict | 600 s | rounded lat,lon |
| Incidents | in-process dict | 120 s | bbox |
| Road graph | memory + disk + baked | bbox-coverage check | bbox |

Signature-based keys mean a forecast is recomputed only when the event materially changes, not on every poll.

---

## 14. Security & multi-tenancy

- **Multi-tenancy:** `tenant_id` on tenants/users/plans/audit; headers `X-Tenant-Id`, `X-User-Id`, `X-User-Role` carry request context.
- **RBAC:** `require_roles(...)` guards privileged endpoints (e.g., `POST /models/retrain`, retention).
- **Audit:** every sensitive action appends to `audit_log` (append-only, per-tenant, indexed by time).
- **Secrets:** API keys and `DATABASE_URL` come from the environment only; `.env` is git- and docker-ignored.

> **Honest limitation:** authentication is currently header-based (trusting `X-User-Role`). This is acceptable for a pilot but **must be replaced with real authentication (JWT/OAuth/session) before any public deployment.** See the roadmap.

---

## 15. Observability & operations

- **Health:** `GET /platform/health` (DB connectivity, integration count) — used as the Render health check.
- **Startup diagnostics:** `start.sh` prints `DATABASE_URL set`, `ROAD_GRAPH_MODE`, `MODEL_DIR`, and dist contents.
- **Degradation breadcrumbs:** functions log when they fall back (e.g., `active_events: database unavailable, using live feeds only`).
- **Model health:** `GET /models/drift`, `GET /models/retrain-plan`, `GET /models/backtest`.
- **Business metrics:** `GET /metrics/summary`, `/metrics/roi`, `/metrics/operational`.

---

## 16. Performance characteristics

| Operation | Latency (approx) | Notes |
|-----------|------------------|-------|
| Forecast (cache hit) | ~50 ms | dict lookup |
| Forecast (cache miss) | ~0.5–2 s | ML inference + live context |
| Plan generation | ~0.5–4 s | graph fetch cached; allocation fast |
| Full retrain | ~4 s (current dataset) | severity + 3 quantiles + risk + survival |
| Live weather fetch | <1 s, ≤ once / 600 s / point | TTL-cached |
| WebSocket broadcast | every 5 s | metrics + newly-active events |

---

## 17. Technology stack

| Layer | Technology |
|-------|-----------|
| API | FastAPI 0.4.0, Gunicorn + Uvicorn workers, WebSockets |
| ML | XGBoost / LightGBM, scikit-learn |
| Optimization | Google OR-Tools (CBC) + greedy fallback |
| Geospatial | OSMnx, NetworkX (A\*), haversine utilities |
| Data | PostgreSQL 15, SQLAlchemy 2, pandas |
| Frontend | React 18, Vite, Leaflet |
| Live data | Open-Meteo (weather, opt-in); pluggable live-incident provider (fixtures by default) |
| Deploy | Docker (multi-stage), Render Blueprint |

---

## 18. Limitations & roadmap

**Known limitations (stated honestly):**

- **Auth is header-based** — needs real authentication before production.
- **Synthetic inputs** — police-station inventory is deterministically seeded; GPS speeds and sensor counts are fixtures.
- **Unvalidated coefficients** — operational metrics (e.g., affected-users-per-minute) are estimates, not yet calibrated against ground truth.
- **Modest labeled data** — duration labels and feedback are limited; the learning loop improves this over time.
- **Free-tier deploy trade-off** — the real OSM build can be slow; the app falls back to the demo grid to keep deploys reliable.

**Roadmap:** real authN/Z · calibrated impact coefficients · live GPS/sensor feeds · scheduled/automatic retraining on a dedicated worker · congestion-aware baseline speeds · parameterization for other cities.

---

### Related documentation

| Doc | Contents |
|-----|----------|
| [README.md](README.md) | Project overview & MVP highlights |
| [HOW_TO_RUN.md](HOW_TO_RUN.md) | Local development & configuration |
| [DEPLOY_RENDER.md](DEPLOY_RENDER.md) | Render deployment guide |
| [CLAUDE.md](CLAUDE.md) | Module-by-module data/ML reference |
| `/docs` (runtime) | Interactive OpenAPI explorer |
