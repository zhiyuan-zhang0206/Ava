"""The cluster surface: `/api/cluster/*` status, roster, admin events and
maintenance control, machine pause/resume, the runner bootstrap handshake,
liveness + status panel + stats dashboard, and the ops monitor series.

Package door — no imports, no re-exports; callers use the modules below.
Routers mounted in `gateway/app.py`: `router`, `machine_pause`, `bootstrap`,
`status`, `alert_classes`, `ops_monitor`. Modules:

  - `router.py`          — `/api/cluster/*` control + admin endpoints
  - `machine_pause.py`   — `POST /api/cluster/machines/{name}/pause|resume`
  - `bootstrap.py`       — `GET /api/bootstrap` runner registration handshake
  - `status.py`          — `/api/health`, `/api/status`, `/api/stats/dashboard`
  - `alert_classes.py`   — `/api/stats/alert-classes[/samples]`: the sidebar card's warning/error
                            classes and one class's newest events
  - `ops_monitor.py`     — `GET /api/ops/monitor`
  - `ops_series.py`     — the query core behind the ops monitor (over `telemetry_events`)
  - `roster_probe.py`    — bounded transport policy for runner status probes
                            (also used by the extensions inventory fan-out)
  - `_roster_rows.py`    — roster row shapers + stamping
  - `_health.py`         — the health endpoint body
  - `_stats_events.py`   — the stats dashboard's window totals over `telemetry_events`
  - `schemas.py`         — the surface's wire models
"""
