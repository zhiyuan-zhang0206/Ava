"""The existing timeline API, exposed unchanged as timeline/context in the CLI."""

from __future__ import annotations

import json


def cmd_agents_timeline(agent_id: int, limit: int | None = None, before: str | None = None) -> int:
    """Print the timeline's standing context and history window as JSON.

    Without ``limit`` the request omits it and the gateway applies its
    configured default window (display.timeline_default_limit)."""
    from shared.http_dial import get
    from shared.machine import gateway_api_base, gateway_auth_headers

    params: dict[str, int | str] = {}
    if limit is not None:
        params["limit"] = limit
    if before is not None:
        params["before"] = before
    response = get(
        f"{gateway_api_base()}/api/agents/{agent_id}/timeline",
        params=params,
        headers=gateway_auth_headers(),
        timeout=30.0,
    )
    response.raise_for_status()
    print(json.dumps(response.json(), ensure_ascii=False))
    return 0
