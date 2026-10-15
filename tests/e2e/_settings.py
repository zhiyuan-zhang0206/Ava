from __future__ import annotations

import httpx


def pin_expand_runs_all(gateway_url: str) -> None:
    resp = httpx.put(
        f"{gateway_url}/api/settings/display.expand_runs_mode",
        json={"value": "all"},
        timeout=30.0,
    )
    resp.raise_for_status()


def pin_compact_history_off(gateway_url: str) -> None:
    resp = httpx.put(
        f"{gateway_url}/api/settings/display.compact_history_sessions",
        json={"value": 0},
        timeout=30.0,
    )
    resp.raise_for_status()
