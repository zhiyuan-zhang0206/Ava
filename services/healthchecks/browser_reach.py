"""Browser reach healthcheck — does the shared browser's own network face reach the gateway?

Run by the agent-runner watchdog (internally throttled,
``settings.services.browser_reach_probe_interval_s``).

The ``browser`` and ``browser_mcp`` checks certify process identity and
supervision, and both stayed green through the 2026-09-18 macmini incident
(task #3921) while every page-level request from the shared Chrome to the app
host hung — stale tabs held the profile's connection pool after a gateway
restart, so navigations and fetches never settled while a same-machine ``curl``
answered in 0.1s. Nothing traversed the browser's network face; this check does,
with two signals because either alone lies:

- **browser path** — a canary fetch through the browser itself: CDP creates a
  background ``about:blank`` target, runs a ``no-cors`` fetch of the gateway
  health URL in it under a wall-clock deadline, and closes the target in
  ``finally`` — the canary never becomes a new occupant.
- **host path** — the same URL read with urllib from this process, the
  "same-machine curl" contrast.

A failing browser path with a healthy host path is the pool/hang shape this
check reports; when the host path fails too, the outage is the gateway's story
and the canary stays quiet. Reporting is episode-gated: one ERROR after
``browser_reach_failure_threshold`` consecutive failing probes, carrying both
raw readings and the recovery recipe pointer, then silence until a healthy
probe logs recovery and re-arms. The check never respawns anything — a browser
respawn would clear the user's tabs, the opposite of the remedy.
"""

from __future__ import annotations

import asyncio
import itertools
import json
import logging
import time
import urllib.error
import urllib.request
from contextlib import suppress
from typing import Any, NamedTuple, cast

import websockets

from services.browser.probe import cdp_url, probe_browser
from shared.config import settings
from shared.log import init_gateway_process

_log = logging.getLogger("services.healthchecks.browser_reach")

# Where the reader of the one ERROR line finds the full incident write-up and
# the manual recovery steps.
_RECIPE_POINTER = "infra/machines/macmini/browser/macmini-shared-chrome-20016-pool-hang-20260918"

# Per-watchdog-process state: the throttle clock and the failure episode span
# rounds in a long-lived watchdog; a restart simply re-arms (conservative).
# brew_pin.py carries the same one-global pattern.
_last_probe_monotonic: float | None = None
_consecutive_failures: int = 0
_reported: bool = False

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


def main() -> None:
    global _last_probe_monotonic, _consecutive_failures, _reported  # noqa: PLW0603 — state spans watchdog rounds

    init_gateway_process(name="browser_reach-healthcheck")

    interval_s = settings.services.browser_reach_probe_interval_s
    now = time.monotonic()
    if (
        interval_s > 0
        and _last_probe_monotonic is not None
        and now - _last_probe_monotonic < interval_s
    ):
        return
    _last_probe_monotonic = now

    url = settings.services.gateway_health_url.strip()
    if not url:
        return

    probe = probe_browser()
    if not probe.alive:
        # The browser check owns liveness/identity; with no probe-alive browser
        # there is nothing to measure, and browser downtime must not count as
        # canary failures (a rebuild also clears the hang this check reports).
        _consecutive_failures = 0
        _log.debug(
            "[browser-reach healthcheck] browser not probe-alive (%s); canary skipped",
            probe.detail,
        )
        return

    canary = _canary(
        settings.services.browser_cdp_port, url, settings.services.browser_reach_timeout_s
    )
    if canary.outcome == "skip":
        _log.debug("[browser-reach healthcheck] canary skipped: %s", canary.detail)
        return
    if canary.outcome == "ok":
        _consecutive_failures = 0
        if _reported:
            _reported = False
            _log.info("[browser-reach healthcheck] browser reach recovered (%s)", canary.detail)
        return

    host = _host_probe(url, settings.services.browser_reach_timeout_s)
    if not host.ok:
        # The host path fails too: a gateway/network outage, another check's
        # story. Do not accumulate the browser-facing count against it.
        _consecutive_failures = 0
        _log.debug(
            "[browser-reach healthcheck] both paths failing (browser=%s; host=%s) — "
            "host-side outage, not counting",
            canary.detail,
            host.detail,
        )
        return

    _consecutive_failures += 1
    threshold = settings.services.browser_reach_failure_threshold
    if _consecutive_failures < threshold:
        _log.debug(
            "[browser-reach healthcheck] canary failing %d/%d (browser=%s; host=%s)",
            _consecutive_failures,
            threshold,
            canary.detail,
            host.detail,
        )
        return
    if _reported:
        _log.debug(
            "[browser-reach healthcheck] still failing (already reported): %s", canary.detail
        )
        return
    _reported = True
    _log.error(
        "[browser-reach healthcheck] the browser cannot reach the gateway (%d consecutive "
        "probes): browser path=%s; host path=%s — the pool/hang shape, not a host outage. "
        "Recovery: close the stale tabs holding %s (recipe: %s). No automatic action was "
        "taken (a browser respawn would clear the user's tabs).",
        _consecutive_failures,
        canary.detail,
        host.detail,
        url,
        _RECIPE_POINTER,
    )


if __name__ == "__main__":
    main()
