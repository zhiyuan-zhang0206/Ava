"""The system -> human alert store: `/api/alerts` ingest (Grafana webhook),
list, SSE tail and read marks, plus the reconciliation loop that repairs rows
whose resolution webhook was lost.

Package door — no imports, no re-exports; callers use the modules below:

  - `router.py`         — mounted endpoints (also `publish_alert_rows` for in-process raisers)
  - `reconciliation.py` — startup + periodic Grafana active-instance reconciliation (lifespan task)
  - `schemas.py`        — the alert wire models
"""
