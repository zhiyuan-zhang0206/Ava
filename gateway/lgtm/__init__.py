"""The gateway's LGTM read side: the live Loki event read, Prometheus aggregates, the
shared query budget, the telemetry-staleness guard, and the retriable 503 an
unavailable read backend maps to.

Package door — no imports, no re-exports; callers use the public modules:

  - `loki_events.py`         — the live Loki event read (`query_events`, the backfill scripts' source)
  - `loki_query_budget.py`   — the process Loki query budget + its telemetry adapter
  - `prom_metrics.py`        — Prometheus-backed telemetry aggregates
  - `telemetry_staleness.py` — heartbeat guard for stale Loki / Prometheus reads
  - `backend_failure.py`     — the consistent retriable 503 for an unavailable read backend

`_loki_*` modules are `loki_events`' private implementation.
"""
