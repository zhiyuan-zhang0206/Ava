"""The gateway's LGTM read side: the telemetry-staleness guard and the retriable
503 an unavailable read backend maps to.

Package door — no imports, no re-exports; callers use the public modules:

  - `telemetry_staleness.py` — heartbeat guard over `telemetry_events`
  - `backend_failure.py`     — the consistent retriable 503 for an unavailable read backend
"""
