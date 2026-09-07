"""Pause-exemption policy — doorplate ② (R3).

The single consumer of the pause side of the doorplate wall: the gateway
middleware asks `should_bypass_pause(path)` and nothing else decides.
Exemption is a route-declared attribute (`PauseSemantics.CONTROL_PLANE`
in `shared/contracts.py`), never a string special-case in middleware — so
the exempt surface is enumerable and auditable. `control_plane_surface()`
exposes it; `tests/gateway/test_route_contracts.py` asserts the exact set,
so a new exemption is a deliberate, reviewed change, not an incident
patch.

Why these routes are control-plane (the audit trail that used to live in
middleware comments):
- `/api/cluster/*` — the gateway's phase-B / observability entry during a
  rollout.
- `/api/alerts` — the alert webhook (Grafana embedded Alertmanager); a 503
  inside the rollout window exhausts Grafana's webhook retries and the
  alert is lost exactly when the user needs alerting most.
"""

from __future__ import annotations

from shared import contracts
from shared.contracts import PauseSemantics


def should_bypass_pause(method: str, path: str) -> bool:
    """Whether ``(method, path)`` may be served while the cluster is paused.

    A route is exempt iff its contract declares CONTROL_PLANE — the
    contract is the single fact source; this function only consumes it.
    The method is part of the key: two methods can share one path template
    with different pause semantics (POST /api/alerts is the control-plane
    webhook; GET /api/alerts is ordinary data-plane).
    """
    return contracts.exempt_from_pause(method, path)


def control_plane_surface() -> frozenset[tuple[str, str]]:
    """The enumerable exempt surface: (method, path template) pairs whose
    contracts declare CONTROL_PLANE.

    The pause middleware's behavior is exactly this set — tests assert the
    surface and the middleware agree, so adding or removing an exemption is
    an auditable change instead of an invisible string edit.
    """
    return frozenset(
        (method, tpl)
        for (method, tpl), c in contracts.ROUTE_CONTRACTS.items()
        if c.pause is PauseSemantics.CONTROL_PLANE
    )
