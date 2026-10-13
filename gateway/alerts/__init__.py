"""The system -> human alert store: `/api/alerts` ingest (Grafana webhook),
list, SSE tail and read marks. The loop that repairs rows whose resolution webhook
was lost is a loop of the events-maintenance service.

Package door — no imports, no re-exports; callers use the modules below:

  - `router.py`         — mounted endpoints
  - `schemas.py`        — the alert wire models (the row and its SSE publish: `ops/alerts.py`)
"""
