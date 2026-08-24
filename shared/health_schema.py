"""The shared health envelope lets a watchdog name the component it must restart.

Health endpoints answer one operational question across every daemon. Keeping the
serialization here prevents one endpoint's HTTP 200 from concealing a stale worker
that another endpoint would expose to its watchdog.
"""

from __future__ import annotations

import json

OK = "ok"
DEGRADED = "degraded"
DOWN = "down"

_COMPONENT_STATUSES = frozenset((OK, DEGRADED, DOWN))


def component(
    name: str,
    status: str,
    *,
    last_success: float | None = None,
    last_error: float | str | None = None,
    progress: str | None = None,
    detail: str | None = None,
    now: float | None = None,
) -> dict[str, object]:
    """Build one component record while omitting facts the daemon has not observed.

    A missing timestamp is deliberately different from a zero timestamp: a newly
    started scheduler must report that it has no completed job yet, not invent an
    age from process birth.
    """
    if status not in _COMPONENT_STATUSES:
        raise ValueError(f"unknown health component status: {status!r}")
    result: dict[str, object] = {"name": name, "status": status}
    if last_success is not None:
        result["last_success"] = last_success
        if now is not None:
            result["age_s"] = round(now - last_success, 1)
    if last_error is not None:
        result["last_error"] = last_error
    if progress is not None:
        result["progress"] = progress
    if detail is not None:
        result["detail"] = detail
    return result


def render(
    identity: dict[str, object],
    components: list[dict[str, object]],
    *,
    liveness: Liveness | LivenessGroup | None = None,
    stale_for: float | None = None,
    extra: dict[str, object] | None = None,
) -> tuple[int, bytes]:
    """Render the uniform response and derive its status from worst component state.

    Identity fields are caller-owned compatibility surface, so a caller-supplied
    value always wins over the derived envelope. ``stale_for`` is an explicit
    stale signal for health sources without a ``Liveness`` or
    ``LivenessGroup`` instance.
    """
    from shared.daemon_health import Liveness, LivenessGroup

    reasons = [
        f"{record['name']}: {record['detail'] if 'detail' in record else record['status']}"
        for record in components
        if record["status"] != OK
    ]
    stale = stale_for is not None or (liveness is not None and not liveness.is_alive())
    healthy = not reasons and not stale
    payload = dict(identity)
    payload.setdefault("status", OK if healthy else DEGRADED)
    if liveness is not None or stale_for is not None:
        payload.setdefault("liveness", "stale" if stale else OK)
    payload.setdefault("readiness", OK if healthy else DEGRADED)
    payload.setdefault("components", components)
    payload.setdefault("degraded_reasons", reasons)
    if extra is not None:
        for key, value in extra.items():
            payload.setdefault(key, value)
    return 200 if healthy else 503, json.dumps(payload).encode("utf-8")
