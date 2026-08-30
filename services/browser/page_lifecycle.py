"""Agent-owned page lifecycle: release on terminate + dead-page sweep.

The browser-mcp daemon (``services.browser.mcp_daemon``) keys one Chrome page
per agent (``_AGENT_AFFINITY``). A worker agent opens a tab on its first
navigate; when the agent terminates it must not leave that tab (usually a dev
server pointing at a dead localhost port) in the user's shared Chrome. Two
mechanisms, both scoped to agent-owned pages only:

- ``release_agent_page`` — the deterministic close: the agent's process-exit
  hook sends a ``release_agent_page`` wire request and the daemon closes that
  agent's page immediately (wire handler ``handle_release_agent_page``).
- ``dead_page_reaper`` — the safety net: a periodic sweep closes agent-owned
  pages whose URL is localhost / 127.0.0.1 with nothing listening on the port.
  This covers the deaths that never reach the exit hook (SIGKILL / OOM /
  force-terminate) and dev servers that died under a still-alive agent.

Everything here is strictly scoped: only pages with a slot in
``_AGENT_AFFINITY`` are ever inspected or closed, so the user's tabs and other
agents' tabs cannot be touched. The registry is module-level so it survives
``ChromeMcpDaemon`` replacement across upstream reconnects (the daemon
re-imports it; the object is shared).
"""

from __future__ import annotations

import asyncio
import re
from contextlib import suppress
from typing import Any, Protocol
from urllib.parse import urlsplit

from mcp import types

from services.browser.protocol import Request, Response
from shared.log import logger

# Per-agent page affinity — agent id -> that agent's current page. The
# selected-page state belongs to the AGENT, not to a TCP connection: an exec
# subprocess child re-connecting mid-turn (or the agent process itself after a
# session rebuild) must land on the same tab the agent selected, not cold-start
# with "No page selected" on every exec. Module-level on purpose: the
# ChromeMcpDaemon object is replaced on upstream reconnect, and this registry
# must survive that. One int per agent that has used the browser — bounded by
# the machine's agent count; a closed/crashed page drops the slot naturally
# via the existing re-pin failure path. Requests without an agent id (legacy
# wrapper clients) keep the per-connection fallback in the daemon.
_AGENT_AFFINITY: dict[int, int | None] = {}

# Dead-page sweep cadence: agent-owned pages pointing at localhost /
# 127.0.0.1 with nothing listening on the port are closed on this pass. Ten
# minutes is far inside the hours a leaked tab sits, and the probe cost is one
# TCP connect per candidate every sweep.
_DEAD_PAGE_SWEEP_INTERVAL_S = 600.0

# Port-probe bound for the sweep. A slow-but-alive listener must never read as
# dead — closing a live tab is the one failure mode that hurts — so anything
# the probe cannot decide within this window is treated as alive.
_PORT_PROBE_TIMEOUT_S = 1.0

# Hosts that name this machine's loopback interface (the dev-server tabs
# worker agents point at). A page on any other host is never a sweep candidate,
# even when its port answers nothing.
_LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1"})

# Page-list line shape: `  <id>: <url> [selected]` — management tools render
# ids and URLs from the shared page namespace; `[selected]` marks the active
# tab and is optional in the parse.
_PAGE_LINE_RE = re.compile(r"^\s*(\d+):\s*(\S+)(?:\s+\[selected\])?\s*$")


class _PageDaemon(Protocol):
    """The slice of ``ChromeMcpDaemon`` the page-lifecycle helpers use.

    A structural protocol keeps this module free of a circular import with the
    daemon (which imports this module for the reaper + wire handling) while
    staying pyright-clean.
    """

    _lock: asyncio.Lock

    async def _call(self, name: str, args: dict[str, Any]) -> types.CallToolResult: ...


def _text_of(result: types.CallToolResult) -> str:
    return "".join(c.text for c in result.content if isinstance(c, types.TextContent))


async def release_agent_page(daemon: _PageDaemon, agent_id: int) -> int | None:
    """Close the page the agent owns and drop its affinity slot.

    Called when the agent process exits (wire method ``release_agent_page``,
    sent by the agent's exit hook) and by the dead-page reaper. Idempotent: an
    agent with no slot (never used the browser, or already released) is a
    no-op. Only the exact page id is closed — never the globally selected page
    — so no other agent's or the user's tab can be affected.
    """
    async with daemon._lock:
        page_id = _AGENT_AFFINITY.get(agent_id)
        if page_id is None:
            return None
        result = await daemon._call("close_page", {"pageId": page_id})
        # A close of an already-gone page errors upstream; either way the slot
        # is released — this agent is done with the browser. Only a transport
        # failure (upstream down) aborts the release, and then the exception
        # propagates and the slot survives for the reaper to retry.
        if result.is_error:
            logger.warning(
                f"[browser-mcp] release of agent {agent_id} page {page_id} "
                f"errored ({_text_of(result)!r}); clearing the slot"
            )
        _AGENT_AFFINITY[agent_id] = None
        return page_id


async def handle_release_agent_page(daemon: _PageDaemon, req: Request, req_id: Any) -> Response:
    """Close the agent's affinity page on a ``release_agent_page`` wire request.

    The request is a terminated agent's exit hook telling the service it is
    done with the browser; the reply names the page that was closed (None when
    the agent had none). A missing/aliased agent id is rejected at the protocol
    edge — same guard as the daemon's ``call_tool``, so a JSON ``true`` can
    never alias another agent's slot.
    """
    agent_id = req.get("agent_id")
    if isinstance(agent_id, int) and not isinstance(agent_id, bool):
        page_id = await release_agent_page(daemon, agent_id)
        return {"id": req_id, "ok": True, "result": {"page_id": page_id}}
    return {
        "id": req_id,
        "ok": False,
        "error": "release_agent_page requires a valid 'agent_id'",
    }


def parse_page_listing(text: str) -> dict[int, str]:
    """page id -> URL from a list_pages / new_page listing result.

    Best-recovery parse: a line that drifts off the shape is skipped; an empty
    dict means nothing matched and the caller treats that as no candidates.
    """
    pages: dict[int, str] = {}
    for line in text.splitlines():
        m = _PAGE_LINE_RE.match(line)
        if m:
            pages[int(m.group(1))] = m.group(2)
    return pages


async def port_listening(host: str, port: int) -> bool:
    """True when something accepts on host:port — or when the probe cannot tell.

    A connect refusal / transport error is definitive proof the port is dead; a
    timeout means something exists but is slow, so conservatively report True —
    never close a live tab on a probe that could not decide. The probe is a bare
    TCP connect; no bytes are sent.
    """
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port), timeout=_PORT_PROBE_TIMEOUT_S
        )
    except TimeoutError:
        # TimeoutError is an OSError subclass — it must be caught BEFORE the
        # transport-error branch: a stuck-but-present listener is not dead.
        return True
    except (ConnectionError, OSError):
        return False
    writer.close()
    with suppress(Exception):
        await writer.wait_closed()
    del reader  # nothing to close on a StreamReader; the writer close suffices
    return True


def local_host_port(url: str) -> tuple[str, int] | None:
    """(host, port) when `url` is an http(s) URL on localhost / 127.0.0.1 —
    the only class the dead-page sweep touches — else None."""
    try:
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https") or parts.hostname not in _LOCAL_HOSTS:
            return None
        port = parts.port or (443 if parts.scheme == "https" else 80)
        return parts.hostname, port
    except ValueError:
        return None


async def reap_dead_agent_pages(daemon: _PageDaemon) -> None:
    """One sweep pass: close agent-owned pages whose URL is a dead local URL.

    Only pages with a slot in ``_AGENT_AFFINITY`` are candidates — user tabs and
    other agents' tabs are never inspected or touched. Two leak classes are
    cleaned here: an agent killed without reaching its exit hook (SIGKILL /
    force-terminate / OOM — the hook can't fire, the slot stays), and a dev
    server that died under a still-alive agent (the tab is a dead link either
    way, and the agent re-opens its tab on the next navigate). A port the probe
    cannot confirm dead stays open.

    Lock discipline: the port probes run OUTSIDE the serial lock (a pass with
    many stale slots must not stall every agent's browser call for seconds),
    and the closes re-read the page list under the lock first — a page the
    agent navigated to a live target while the probe ran, or already closed,
    is not touched. The slot is cleared only when it still names the closed
    page, so a live page the agent opened meanwhile keeps its affinity.
    """
    async with daemon._lock:
        listing = await daemon._call("list_pages", {})
        if listing.is_error:
            return
        page_urls = parse_page_listing(_text_of(listing))
        candidates: list[tuple[int, int, str]] = []
        for agent_id, page_id in list(_AGENT_AFFINITY.items()):
            if page_id is None:
                continue
            url = page_urls.get(page_id)
            if url is None:
                continue  # already closed upstream — the slot drops on next use
            target = local_host_port(url)
            if target is None:
                continue
            candidates.append((agent_id, page_id, url))
    if not candidates:
        return
    dead: list[tuple[int, int, str]] = []
    for agent_id, page_id, url in candidates:
        target = local_host_port(url)
        if target is None:
            continue
        host, port = target
        if await port_listening(host, port):
            continue
        dead.append((agent_id, page_id, url))
    if not dead:
        return
    async with daemon._lock:
        listing = await daemon._call("list_pages", {})
        if listing.is_error:
            return
        page_urls = parse_page_listing(_text_of(listing))
        for agent_id, page_id, url in dead:
            if page_urls.get(page_id) != url:
                continue  # re-purposed to a live target (or closed) mid-pass
            result = await daemon._call("close_page", {"pageId": page_id})
            # The page is dead; clear the slot whatever the close says. On an
            # error the page may actually still be open (rare), so say so.
            if result.is_error:
                logger.warning(
                    f"[browser-mcp] reaper could not close dead page {page_id} ({url}): "
                    f"{_text_of(result)!r}"
                )
            if _AGENT_AFFINITY.get(agent_id) == page_id:
                _AGENT_AFFINITY[agent_id] = None
            logger.info(
                f"[browser-mcp] reaper closed dead page {page_id} ({url}) for agent {agent_id}"
            )


async def dead_page_reaper(daemon: _PageDaemon, stop: asyncio.Event) -> None:
    """Periodically sweep agent-owned dead localhost pages (see
    ``reap_dead_agent_pages``). Runs per upstream connection like the daemon's
    watchdog; a pass that hits upstream death returns so the daemon reconnects.
    """
    while not stop.is_set():
        with suppress(asyncio.TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=_DEAD_PAGE_SWEEP_INTERVAL_S)
        if stop.is_set():
            return
        try:
            await reap_dead_agent_pages(daemon)
        except RuntimeError:
            # Upstream died mid-pass — daemon.dead is set; the daemon reconnects
            # and recreates this task on the new connection.
            return
        except Exception as e:
            # One bad pass must not kill the reaper.
            logger.warning(f"[browser-mcp] dead-page reaper pass failed: {e!r}")
