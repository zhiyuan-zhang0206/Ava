"""Schedules API: `/api/schedules` CRUD + control, and `session_control`, the API's
way to reach the schedule-manager service that keeps each schedule's resident
session alive (`services/wake/schedule_manager`).

Package door — no imports, no re-exports; callers use the modules below:

  - `router.py`  — `/api/schedules/*` endpoints (mounted in `gateway/app.py`)
  - `session_control.py` — queued sync requests + log capture (the service itself
    is `services/wake/schedule_manager`)

`runner.py` owns script execution and the stall guard. The independent process
entrypoint is `services.wake.schedule_manager.runner`, which supplies the SDK
actor binding and plugin loading explicitly.
"""
