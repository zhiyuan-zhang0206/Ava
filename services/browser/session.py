"""Mint and inject the gateway session cookie into the shared managed Chrome.

The gateway reverse-proxies agent-served pages under auth (session cookie or
Bearer token), so the managed browser needs its own ``ava_session`` cookie to
open page URLs. The session token is a pure HMAC function of the cluster
secret (``shared.cluster_auth.sign_session``) — no login round-trip, no rate
limiter — so this module mints it locally and injects it into Chrome over CDP.

Injection path: ``Network.setCookie`` on a page target's websocket. Chrome
only exposes the Network domain on page targets (the browser-level target
answers ``-32601``), so we create a throwaway ``about:blank`` tab through the
browser-level websocket, set the cookie on it, and close it again. The cookie
is host-only for the configured gateway URL (no Domain attribute, matching the
login cookie), so it is never sent to any other host.

Runs inside the browser-mcp daemon process, which shares the machine with
Chrome and the cluster secret.
"""

from __future__ import annotations

import asyncio
import base64
import itertools
import json
from contextlib import suppress
from typing import Any

import httpx
import websockets

from shared.cluster_auth import cookie_name, sign_session

# CDP endpoint timeouts: Chrome is local, so these only bound a wedged browser.
_HTTP_TIMEOUT_S = 5.0
_WS_TIMEOUT_S = 10.0

# IDs are per-connection in practice, but a monotonically increasing global id
# is valid on any connection too and keeps the helper stateless.
_CDP_IDS = itertools.count(1)


def gateway_session_cookie(secret: str) -> tuple[str, str, int]:
    """(name, value, expires_unix) for the gateway session cookie.

    ``expires_unix`` is decoded from the freshly signed token itself, so the
    cookie's expiry always mirrors the token's embedded expiry — they die
    together even if ``sign_session``'s default TTL ever changes.
    """
    name = cookie_name()
    value = sign_session(secret)
    padded = value + "=" * (-len(value) % 4)
    expiry = int(base64.urlsafe_b64decode(padded).split(b".", 1)[0])
    return name, value, expiry


async def _http_json(url: str) -> Any:
    """GET ``url`` and return the parsed JSON body."""
    async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT_S) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        return resp.json()


async def _cdp_call(ws: Any, method: str, params: dict[str, Any]) -> dict[str, Any]:
    """Send one CDP request on an open websocket and return its ``result``.

    Raises RuntimeError on a protocol-level error response.
    """
    await ws.send(json.dumps({"id": next(_CDP_IDS), "method": method, "params": params}))
    raw = await asyncio.wait_for(ws.recv(), timeout=_WS_TIMEOUT_S)
    resp = json.loads(raw)
    if "error" in resp:
        raise RuntimeError(f"CDP {method} failed: {resp['error']}")
    return resp.get("result") or {}


async def inject_session_cookie(cdp_port: int, gateway_url: str, secret: str) -> None:
    """Mint a gateway session cookie and set it in the managed Chrome.

    The cookie is host-only for ``gateway_url`` with the same flags the login
    flow sets (HttpOnly, SameSite=Lax, Path=/, expiry mirrored to the token).

    Raises RuntimeError when Chrome is unreachable or rejects the cookie.
    """
    name, value, expires = gateway_session_cookie(secret)
    version = await _http_json(f"http://127.0.0.1:{cdp_port}/json/version")
    browser_ws_url = version["webSocketDebuggerUrl"]

    async with websockets.connect(browser_ws_url) as browser_ws:
        # A page target is required for the Network domain; use a throwaway
        # about:blank tab so we never depend on (or disturb) anyone's tabs.
        created = await _cdp_call(browser_ws, "Target.createTarget", {"url": "about:blank"})
        target_id = created["targetId"]
        try:
            page_ws_url = f"ws://127.0.0.1:{cdp_port}/devtools/page/{target_id}"
            async with websockets.connect(page_ws_url) as page_ws:
                result = await _cdp_call(
                    page_ws,
                    "Network.setCookie",
                    {
                        "name": name,
                        "value": value,
                        "url": gateway_url.rstrip("/") + "/",
                        "path": "/",
                        "httpOnly": True,
                        "sameSite": "Lax",
                        "expires": expires,
                    },
                )
        finally:
            with suppress(Exception):
                await _cdp_call(browser_ws, "Target.closeTarget", {"targetId": target_id})

    if not result.get("success"):
        raise RuntimeError(f"Chrome rejected the gateway session cookie: {result}")
