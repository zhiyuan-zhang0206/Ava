"""Frontend telemetry ingestion — `POST /api/frontend-telemetry`.

The web frontend's user-modeling telemetry (`frontend/src/lib/telemetry.ts`)
batches tracked interactions — key-control clicks, page views, user_settings
changes — and posts them here; this router validates the batch and emits one
`frontend_interaction` event (category=telemetry, source=user) per accepted
interaction into the unified event stream (shared/telemetry.py). The events
table is the store; the Grafana core-metrics panels aggregate it.

Two volume guards sit between the browser and the events table:

- the client already dedupes (2 s window per page/element) and rate-limits
  itself (100 events/min), so the honest volume is ~1-2k rows/day;
- this router backstops that with a per-session sliding-window cap
  (120 events/min, in-memory — one gateway process) so a misbehaving tab
  (infinite loop, retry storm) cannot blow up the table. Excess events are
  dropped and counted in the warning log, never retried.

Content rules: no free text crosses this boundary. `element`/`page` are
fixed vocabularies maintained in the frontend module; `key`/`value` carry a
user_settings key and a sanitized scalar rendering of its new value
(bool / number / ≤64-char string) on setting-change events only. The client
timestamp is advisory and ignored — the server stamps rows with its own
clock (one time source for the whole stream).
"""

from __future__ import annotations

import logging
import time
from collections import deque
from typing import Any

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse

from gateway.schemas.frontend_telemetry import FrontendInteractionIn, FrontendTelemetryBatch
from shared import telemetry

router = APIRouter()

_log = logging.getLogger(__name__)

# Backstop rate limit: events per session per minute (sliding window). The
# client caps itself at 100/min; this is defense in depth, not the primary
# throttle.
_MAX_EVENTS_PER_MINUTE = 120
# Sessions tracked at once; past this the oldest-touched session is evicted
# (a fresh window — a restarting tab just gets a fresh budget).
_MAX_SESSIONS = 512
# Batch byte cap: 200 events x ~400 B worst case is ~80 KB; 64 KB sits
# comfortably above the honest ceiling.
_MAX_BODY_BYTES = 65_536

# Sliding windows: session_id -> deque of event timestamps (monotonic-ish
# wall clock). Module state is fine — one gateway process; a restart resets
# every window, which only loosens the backstop briefly.
_session_windows: dict[str, deque[float]] = {}


def _rate_limited(session_id: str, now: float) -> bool:
    """True when `session_id` has spent its per-minute event budget."""
    win: deque[float] | None = _session_windows.get(session_id)
    if win is None:
        if len(_session_windows) >= _MAX_SESSIONS:
            _session_windows.pop(next(iter(_session_windows)))
        win = deque()
        _session_windows[session_id] = win
    while win and now - win[0] > 60:
        win.popleft()
    if len(win) >= _MAX_EVENTS_PER_MINUTE:
        return True
    win.append(now)
    return False


def _emit_one(batch: FrontendTelemetryBatch, ev: FrontendInteractionIn) -> None:
    """Emit one validated interaction as a `frontend_interaction` event."""
    attributes: dict[str, Any] = {
        "page": ev.page,
        "element": ev.element,
        "session_id": batch.session_id,
    }
    if ev.key is not None:
        attributes["key"] = ev.key
    if ev.value is not None:
        attributes["value"] = ev.value
    telemetry.emit(
        "telemetry",
        "frontend_interaction",
        level="info",
        source="user",
        attributes=attributes,
    )


@router.post("/api/frontend-telemetry")
async def ingest(request: Request) -> Response:
    """Validate one telemetry batch and emit its accepted events (204).

    Best-effort by contract: the client never depends on this call. 422 on a
    malformed batch (a bug in our own client — fail fast, no silent shape
    drift), 413 past the byte cap, 204 with a warning log when the session
    rate limit drops part of the batch.
    """
    raw = await request.body()
    if len(raw) > _MAX_BODY_BYTES:
        return JSONResponse(status_code=413, content={"detail": "batch too large"})
    try:
        batch = FrontendTelemetryBatch.model_validate_json(raw)
    except Exception as exc:  # validation errors are pydantic's, shape is ours
        return JSONResponse(
            status_code=422,
            content={"detail": f"invalid telemetry batch: {exc}"},
        )

    now = time.time()
    accepted = 0
    dropped = 0
    for ev in batch.events:
        if _rate_limited(batch.session_id, now):
            dropped += 1
            continue
        _emit_one(batch, ev)
        accepted += 1
    if dropped:
        _log.warning(
            "frontend telemetry: dropped %d/%d events for session %s (rate limit)",
            dropped,
            len(batch.events),
            batch.session_id,
        )
    return Response(status_code=204)
