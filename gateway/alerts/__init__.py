"""The system -> human alert store: `/api/alerts` ingest (Grafana webhook),
list, SSE tail and read marks. The loop that repairs rows whose resolution webhook
was lost is a loop of the events-maintenance service.

Package door — no imports, no re-exports; callers use the modules below:

  - `router.py`         — mounted endpoints
  - `publish.py`        — `publish_alert_rows`: SSE publish for every raiser, in-process or not
  - `schemas.py`        — the alert wire models
"""
