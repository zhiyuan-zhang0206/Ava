"""Gateway-hosted schedules: `/api/schedules` CRUD + control and the
ScheduleManager that keeps each schedule's resident session alive.

Package door — no imports, no re-exports; callers use the modules below:

  - `router.py`  — `/api/schedules/*` endpoints (mounted in `gateway/app.py`)
  - `manager.py` — ScheduleManager: session keep-alive + circuit breaker (lifespan task)

The in-session entrypoint stays `gateway/schedule_runner.py`
(`python -m gateway.schedule_runner <id>`, a cross-version launch contract).
"""
