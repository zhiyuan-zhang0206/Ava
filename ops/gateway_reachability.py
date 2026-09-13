"""Host-local gateway reachability evidence for the pause controller.

Split out of `ops.controllers.stranded_pause` unchanged (task #3270) to keep
that module under its line budget; names, seams and semantics are the same.
"""

from __future__ import annotations

import contextlib
import os
import time
from pathlib import Path

# The reachability-evidence grace (value, ordering and rationale live in the
# clock lattice — see `shared.deploy_timing.GATEWAY_DOWN_OWNER_GRACE_S`).
_GATEWAY_DOWN_MARKER = "gateway-down-since"


def _gateway_down_marker_path() -> Path:
    import shared.paths

    return shared.paths.run_dir() / _GATEWAY_DOWN_MARKER


def _probe_gateway_reachable() -> bool:
    """Whether the gateway answers its health URL at all.

    Any HTTP response (even a 503 — the process is alive but degraded) counts as
    reachable: the question is "can the gateway-side orchestration still be
    executing", and a process that answers can be driven. Only connection errors
    and timeouts (the event-loop freeze shape) read as unreachable."""
    import httpx

    from shared.config import settings

    try:
        httpx.get(settings.services.gateway_health_url, timeout=2.0)
    except httpx.HTTPError:
        return False
    return True


def record_gateway_reachability() -> None:
    """Maintain the host-local gateway-down-since marker.

    Called by the pause controller each round on the gateway-capability watchdog.
    Reachable clears the marker; unreachable stamps it once (the FIRST down round
    is the evidence's anchor — a later probe keeps the original timestamp so the
    grace bound measures the continuous outage, not the last failed probe).

    Best-effort on both sides: an unwritable run dir must not break the tick, and
    a missing marker reads as no evidence (the conservative, lease-owns path)."""
    path = _gateway_down_marker_path()
    if _probe_gateway_reachable():
        with contextlib.suppress(OSError):
            path.unlink(missing_ok=True)
        return
    with contextlib.suppress(OSError):
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            return
        tmp = path.with_name(f".{_GATEWAY_DOWN_MARKER}.tmp")
        tmp.write_text(str(time.time()))
        os.replace(tmp, path)  # noqa: PTH105 — explicit atomic replace injection seam


def _gateway_down_seconds() -> float | None:
    """How long the gateway has been unreachable, or None when there is no
    evidence (marker absent, unreadable, or a backwards clock — all read as
    no-evidence so the lease keeps owning the pause)."""
    path = _gateway_down_marker_path()
    try:
        ts = float(path.read_text().strip())
    except (OSError, ValueError):
        return None
    elapsed = time.time() - ts
    return elapsed if elapsed >= 0 else None
