"""Browser network canary and host contrast, scheduled by root diagnostics.

The canary opens one temporary target and closes it in finally. It never stops,
restarts, or repairs the shared browser. Root diagnostics verifies native
listener ancestry before and after invoking these protocol helpers.
"""

from __future__ import annotations

import asyncio
import itertools
import json
import time
import urllib.error
import urllib.request
from contextlib import suppress
from typing import Any, NamedTuple, cast

import websockets

from services.browser.probe import cdp_url

# CDP request ids need only be unique per connection, but a global counter is
# valid anywhere and keeps the helpers stateless.
_CDP_IDS = itertools.count(1)


class _CanaryResult(NamedTuple):
    """One canary round: ``ok``/``timeout``/``error``, or ``skip`` when no
    reachability verdict is possible (CDP control plane unusable)."""

    outcome: str
    detail: str


class _HostResult(NamedTuple):
    ok: bool
    detail: str


def _read_json(url: str, timeout_s: float) -> dict[str, Any]:
    with urllib.request.urlopen(url, timeout=timeout_s) as response:  # noqa: S310 — loopback CDP URL
        parsed: object = json.loads(response.read())
    if not isinstance(parsed, dict):
        return {}
    return cast("dict[str, Any]", parsed)


async def _cdp_call(
    ws: Any, method: str, params: dict[str, Any], timeout_s: float
) -> dict[str, Any]:
    """Send one CDP request and return its ``result``; raises on a protocol error."""
    await ws.send(json.dumps({"id": next(_CDP_IDS), "method": method, "params": params}))
    raw = await asyncio.wait_for(ws.recv(), timeout=timeout_s)
    resp = json.loads(raw)
    if "error" in resp:
        raise RuntimeError(f"CDP {method} failed: {resp['error']}")
    return resp.get("result") or {}


async def _canary_async(port: int, url: str, timeout_s: float) -> _CanaryResult:
    """The browser-path canary: throwaway background target, one fetch, closed."""
    deadline = time.monotonic() + timeout_s

    def left() -> float:
        return max(0.05, deadline - time.monotonic())

    version = await asyncio.to_thread(_read_json, cdp_url(port), left())
    browser_ws_url = version.get("webSocketDebuggerUrl")
    if not isinstance(browser_ws_url, str) or not browser_ws_url:
        return _CanaryResult("skip", "CDP /json/version carried no browser websocket")

    # close_timeout is bounded so a wedged browser cannot stall the round.
    async with websockets.connect(
        browser_ws_url, open_timeout=left(), close_timeout=2.0
    ) as browser_ws:
        created = await _cdp_call(
            browser_ws, "Target.createTarget", {"url": "about:blank", "background": True}, left()
        )
        target_id = created.get("targetId")
        if not isinstance(target_id, str) or not target_id:
            return _CanaryResult("skip", "Target.createTarget returned no targetId")
        try:
            expression = (
                f"fetch({json.dumps(url)}, {{mode: 'no-cors', cache: 'no-store'}})"
                ".then(() => 'ok')"
                ".catch((e) => 'error:' + (e && e.name ? e.name : 'unknown'))"
            )
            page_ws_url = f"ws://127.0.0.1:{port}/devtools/page/{target_id}"
            async with websockets.connect(
                page_ws_url, open_timeout=left(), close_timeout=2.0
            ) as page_ws:
                try:
                    result = await _cdp_call(
                        page_ws,
                        "Runtime.evaluate",
                        {"expression": expression, "awaitPromise": True, "returnByValue": True},
                        left(),
                    )
                except TimeoutError:
                    return _CanaryResult("timeout", f"fetch did not settle within {timeout_s:.1f}s")
            value = cast("dict[str, Any]", result.get("result") or {}).get("value")
            if value == "ok":
                return _CanaryResult("ok", "fetch settled")
            if isinstance(value, str) and value.startswith("error:"):
                return _CanaryResult("error", f"fetch rejected ({value})")
            return _CanaryResult("skip", f"unexpected evaluate payload: {value!r}")
        finally:
            # The canary must never become a new occupant.
            with suppress(Exception):
                await _cdp_call(browser_ws, "Target.closeTarget", {"targetId": target_id}, 2.0)


def _canary(port: int, url: str, timeout_s: float) -> _CanaryResult:
    """Total wrapper: the canary never raises — an unforeseen failure is a
    ``skip`` (no verdict), never a false reachability claim."""
    try:
        return asyncio.run(_canary_async(port, url, timeout_s))
    except Exception as exc:
        return _CanaryResult("skip", f"canary raised {type(exc).__name__}: {exc}")


def _host_probe(url: str, timeout_s: float) -> _HostResult:
    """The same-machine contrast read: any HTTP answer counts as reachability."""
    started = time.monotonic()
    try:
        with urllib.request.urlopen(url, timeout=timeout_s) as response:  # noqa: S310 — cluster URL from settings
            status = response.status
        return _HostResult(ok=True, detail=f"HTTP {status} in {time.monotonic() - started:.2f}s")
    except urllib.error.HTTPError as exc:
        return _HostResult(ok=True, detail=f"HTTP {exc.code} in {time.monotonic() - started:.2f}s")
    except Exception as exc:
        return _HostResult(
            ok=False, detail=f"{type(exc).__name__} after {time.monotonic() - started:.2f}s"
        )
