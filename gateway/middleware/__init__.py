"""The request pipeline around every route: maintenance admission, keyed
idempotency, latency metering, typed error envelopes + exception handlers, the
process runtime metrics, and the shutdown mark long-lived streams poll.

Package door — no imports, no re-exports; `gateway/app.py` wires the modules:

  - `pause_policy.py`    — pause-exemption decision (doorplate ②)
  - `idempotency.py`     — `AT_LEAST_ONCE_WITH_KEY` dedup middleware (doorplate ①)
  - `latency.py`         — per-route latency metering middleware + its flusher
  - `error_envelope.py`  — typed error responses + the request-trace middleware
  - `error_handlers.py`  — exception-to-envelope adapters
  - `runtime_metrics.py` — process, event-loop, and SSE connection metrics
  - `stopping.py`        — the server-is-shutting-down mark long-lived streams poll
"""
