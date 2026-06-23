# Deploying to Render

This project ships a **Render Blueprint** (`render.yaml`) and a `Dockerfile`, so
deploying is mostly one click. It provisions a free PostgreSQL database and a
Docker web service that serves the API + dashboard.

---

## 1. Prerequisites

- The code pushed to a GitHub (or GitLab) repo.
- A free [Render](https://render.com) account.
- (Optional) API keys for live feeds — the app works without them (see step 4).

---

## 2. One-click Blueprint deploy

1. Render dashboard → **New → Blueprint**.
2. Connect the repo containing this project.
3. Render reads `render.yaml` and shows the plan:
   - **`trafficguide-db`** — free PostgreSQL.
   - **`trafficguide`** — Docker web service (built from `./Dockerfile`).
4. Click **Apply**. Render builds the image, provisions the DB, and deploys.

When it finishes, the app is at:

```
https://<your-service>.onrender.com/app          # command console
https://<your-service>.onrender.com/app/field    # field officer view
https://<your-service>.onrender.com/platform/health   # health check
```

`/` redirects to `/app/`.

---

## 3. What happens on first boot

`scripts/start.sh` runs automatically and is idempotent:

1. Waits for the database to accept connections.
2. Applies the schema (`init_db.py`).
3. Loads the bundled Astram events CSV (skips if already populated).
4. Seeds demo feedback so the dashboard has data immediately.
5. Bootstraps the trained models and the offline demo road graph.
6. Starts Gunicorn (Uvicorn worker) bound to Render's `$PORT`.

The health check path is `/platform/health`; Render waits for it before routing
traffic.

---

## 4. Environment variables

`render.yaml` pre-wires the essentials. These are the ones you may want to set
or change in the service's **Environment** tab:

| Variable | Purpose | Default |
|---|---|---|
| `DATABASE_URL` | Injected automatically from the managed DB | _(auto)_ |
| `ROAD_GRAPH_MODE` | `live` = real OSM roads, `demo` = fast offline grid | `live` |
| `MODEL_DIR` | Trained model artifacts | `/app/models` |
| `WEB_CONCURRENCY` | Gunicorn workers (free tier: keep at 1) | `1` |
| `ACTIVE_EVENT_INCLUDE_DEMO_FEEDS` | Blend synthetic "live" incidents | `true` |
| `LIVE_WEATHER` | Real per-event rainfall/wind from Open-Meteo (keyless) | `true` |
| `INCIDENTS_BBOX` | `minLon,minLat,maxLon,maxLat` for incident queries | Bengaluru |

> Live traffic incidents use realistic **fixtures by default**. The integrations
> layer provides a configurable slot to plug in a live-incident API provider;
> when no key is configured the fixtures are served.

**Live feeds are optional.** With no incident key the app uses realistic
fixtures; weather is genuinely live out of the box (no key needed).

---

## 5. The road graph: real vs demo (important for build time)

Diversions can route over the **real Bengaluru OSM network** or a fast offline
**demo grid**.

- **`ROAD_GRAPH_MODE=live` (default):** the Docker build pre-downloads the real
  graph (`scripts/build_graph_cache.py`) and bakes it into the image, so routing
  needs no runtime download. This download can take several minutes and is
  **best-effort** — if it can't finish in the build, the deploy still succeeds
  and the app **automatically falls back to the demo grid at runtime** (it never
  blocks a request on a live download).
- **`ROAD_GRAPH_MODE=demo`:** skips the heavy download entirely for a fast,
  guaranteed build. Diversions use the offline grid. Good for quick demos or if
  the live build is too slow on your plan.

If your first build is slow or you don't need real-street geometry, set
`ROAD_GRAPH_MODE=demo` and redeploy. To shrink the live build instead of
disabling it, set a smaller `BENGALURU_BBOX` (`south,west,north,east`).

---

## 6. Verify the deploy

```bash
curl https://<your-service>.onrender.com/platform/health        # -> {"status": ...}
curl https://<your-service>.onrender.com/metrics/summary          # live metrics
```

Open `/app` — you should see incident markers on the map, the metric strip, and
the executive ROI row populated. Open `/app/field` for the officer view.

---

## 7. Post-event learning (retrain)

Officer feedback (`POST /events/{id}/feedback`) accumulates corrected outcomes.
Retrain on demand:

```bash
curl -X POST https://<your-service>.onrender.com/models/retrain -H "X-User-Role: admin"
curl https://<your-service>.onrender.com/models/retrain/status
```

The job runs in the background and hot-reloads the models — no restart needed.

---

## 8. Troubleshooting

- **Build is very slow / times out** → set `ROAD_GRAPH_MODE=demo` and redeploy.
- **App boots but dashboard is empty** → check logs for the DB init/load step;
  confirm `DATABASE_URL` is wired (it is, via the Blueprint).
- **Incident markers look static** → expected: incidents use fixtures by default.
  Wire a live-incident provider via the integrations layer's API-key slot to go live.
- **First request after a cold start is slow** → Render's free tier spins the
  service down when idle; the first hit wakes it.
- **500s right after deploy** → the DB may still be warming up; `start.sh` waits,
  but a very cold free Postgres can lag. Re-check `/platform/health` after a
  minute.

---

## 9. Updating

`autoDeploy` is enabled, so pushing to the connected branch triggers a new build
and deploy automatically. To deploy a specific branch, change the branch in the
service settings.
