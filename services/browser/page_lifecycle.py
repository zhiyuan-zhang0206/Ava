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
- ``reap_idle_agent_pages`` — the other half of the safety net: agent-owned
  pages whose owner has not touched the browser for ``_TAB_IDLE_TIMEOUT_S``
  are closed. A terminated agent's tab to any URL (not just dead localhost)
  ages out through this pass, because its last-use stamp stops moving; a
  live agent's tab is never cut short as long as it keeps using the browser.
- ``reap_expired_pages`` — the hard deadline (2026-09-11 ruling): every page
  this stack CREATED (an explicit new_page, or the auto-created page on a
  page-less first navigate) is registered in ``_PAGE_TTL_DEADLINES`` with a
  deadline of ``now + AVA_CHROME_PAGE_DEFAULT_TTL_SECONDS`` (24h default), and
  the sweep closes it once that deadline passes. Activity never extends the
  deadline; ``renew_agent_page`` (the ``renew_page`` tool) moves it to
  ``now + ttl`` explicitly, at most 24h per call. This is what bounds a page
  an agent keeps USING across days — the idle sweep never fires for it.

Scoping: the reapers' candidate sources are the affinity registry and the TTL
registry — both contain only pages this stack created — so the user's tabs and
pages this stack never created are never inspected or closed. A user tab the
agent merely selected has no TTL slot and no renewal path; it is out of scope
by construction. The registries are module-level so they survive
``ChromeMcpDaemon`` replacement across upstream reconnects (the daemon
re-imports them; the objects are shared).
"""

from __future__ import annotations

import asyncio
import math
import re
import time
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol
from urllib.parse import urlsplit

from mcp import types

from services.browser.protocol import Request, Response
from shared import telemetry
from shared.config import settings
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

# Monotonic last-use stamp per agent, updated on every agent-keyed browser
# call (``touch_agent_page``). The idle sweep reads it; an agent with no
# affinity page (None) is not a candidate. Module-level for the same reason
# as the affinity registry — it must survive daemon replacement.
_AGENT_LAST_USE: dict[int, float] = {}

# Dead-page sweep cadence: agent-owned pages pointing at localhost /
# 127.0.0.1 with nothing listening on the port are closed on this pass. Ten
# minutes is far inside the hours a leaked tab sits, and the probe cost is one
# TCP connect per candidate every sweep.
_DEAD_PAGE_SWEEP_INTERVAL_S = 600.0

# Port-probe bound for the sweep. A slow-but-alive listener must never read as
# dead — closing a live tab is the one failure mode that hurts — so anything
# the probe cannot decide within this window is treated as alive.
_PORT_PROBE_TIMEOUT_S = 1.0

# Idle timeout for the agent-page sweep: an agent-owned page whose owner has
# gone this long without a browser call is recycled. Six hours keeps an
# actively-working agent untouched (its stamp moves on every call) while
# still reaping the tabs left behind by finished or terminated work the same
# day it ends.
_TAB_IDLE_TIMEOUT_S = 6 * 60 * 60

# Hosts that name this machine's loopback interface (the dev-server tabs
# worker agents point at). A page on any other host is never a sweep candidate,
# even when its port answers nothing.
_LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1"})

# Hard cap on one renewal grant — at most 24h, the same cap persistent shell
# sessions live under (user ruling 2026-09-01). The initial (creation) deadline
# is the configured default, `chrome_page_default_ttl_seconds`.
_MAX_TTL_SECONDS = 86_400.0

# TTL deadline per page — page id -> monotonic deadline. Every page THIS STACK
# created (an explicit new_page call, or the auto-created page on a page-less
# first navigate) gets a slot from ``register_created_page``; the expiry sweep
# closes a page once its deadline passes. Monotonic (never wall clock): a
# clock step must not move a deadline. The registry is the ONLY candidate
# source for the TTL sweep, so pages this stack never created (the user's own
# tabs) are never inspected or closed. Module-level for the same reason as the
# affinity registry: it must survive daemon replacement across upstream
# reconnects.
_PAGE_TTL_DEADLINES: dict[int, float] = {}

# The ``renew_page`` tool this module owns. Lives beside the TTL machinery
# because the daemon appends it to the upstream tool list verbatim passthrough
# (``chrome-devtools-mcp`` has no TTL concept of its own).
RENEW_PAGE_TOOL = "renew_page"

_RENEW_PAGE_DESCRIPTION = (
    "Renew the TTL deadline of this agent's current page. Pages opened through "
    "this browser carry a hard deadline (24h default) after which they are "
    "closed; renewal moves the deadline to now + ttl (default: the configured "
    "page TTL)."
)


def renew_page_tool_dump() -> dict[str, Any]:
    """The daemon-owned ``renew_page`` tool dict, in the same wire shape as the
    upstream tools the daemon passes through verbatim (``types.Tool`` dump):
    appended to ``list_tools`` by ``ChromeMcpDaemon``."""
    return types.Tool(
        name=RENEW_PAGE_TOOL,
        description=_RENEW_PAGE_DESCRIPTION,
        input_schema={
            "type": "object",
            "properties": {
                "ttl": {
                    "type": "number",
                    "description": (
                        "Seconds from now until the new deadline. Defaults to the "
                        "cluster's default page TTL; must be greater than zero and "
                        "at most 86400 (24 hours)."
                    ),
                }
            },
            "required": [],
            "additionalProperties": False,
        },
    ).model_dump(mode="json", by_alias=True)


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
        _AGENT_LAST_USE.pop(agent_id, None)
        drop_page_ttl(page_id)
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
            drop_page_ttl(page_id)
            logger.info(
                f"[browser-mcp] reaper closed dead page {page_id} ({url}) for agent {agent_id}"
            )


def touch_agent_page(agent_id: int) -> None:
    """Stamp the agent's last browser-use time (monotonic).

    Called by the daemon on every agent-keyed call. The stamp is the idle
    signal for :func:`reap_idle_agent_pages`; it is never used to judge the
    user's tabs or any page without an affinity slot.
    """
    _AGENT_LAST_USE[agent_id] = time.monotonic()


def _default_page_ttl_seconds() -> float:
    """The configured default TTL for a newly created page."""
    return float(settings.daemon.chrome_page_default_ttl_seconds)


def _tool_ok(text: str) -> types.CallToolResult:
    return types.CallToolResult(content=[types.TextContent(type="text", text=text)], is_error=False)


def _tool_error(text: str) -> types.CallToolResult:
    return types.CallToolResult(content=[types.TextContent(type="text", text=text)], is_error=True)


def register_created_page(page_id: int | None) -> None:
    """Give a freshly created page its initial TTL deadline.

    Called by the daemon right after a ``new_page`` it forwarded (an explicit
    call, or the auto-created page on a page-less first navigate) resolved to
    a page id. Best-effort like the affinity parse it rides on: a listing
    that drifted off the parseable shape yields ``None`` and the page simply
    carries no TTL. Callers hold the daemon's serial lock.
    """
    if page_id is None:
        return
    _PAGE_TTL_DEADLINES[page_id] = time.monotonic() + _default_page_ttl_seconds()


def drop_page_ttl(page_id: int | None) -> None:
    """Forget the page's TTL slot — the page is closed (or was never ours)."""
    if page_id is not None:
        _PAGE_TTL_DEADLINES.pop(page_id, None)


def _clear_affinity_for_page(page_id: int) -> int | None:
    """Clear every agent slot naming ``page_id`` (the page is gone);

    returns one cleared owner for logs / the expiry event (None when no slot
    named the page). Every slot is cleared — a slot left naming a closed page
    would make the owner's next call re-pin against a dead id first.
    """
    owner: int | None = None
    for agent_id, current in list(_AGENT_AFFINITY.items()):
        if current == page_id:
            _AGENT_AFFINITY[agent_id] = None
            if owner is None:
                owner = agent_id
    return owner


async def renew_agent_page(
    daemon: _PageDaemon, agent_id: int, args: dict[str, Any]
) -> types.CallToolResult:
    """Handle one ``renew_page`` tool call: move the agent page's TTL deadline.

    Semantics mirror ``ava.shell.sessions.renew`` (the shell-TTL ruling): the
    deadline becomes ``now + ttl`` — never stacked on the old one — each call
    grants at most 24h, and an already-passed deadline is NOT renewable (the
    sweep owns the page; there is no renewable expired state). ``ttl`` is
    optional and defaults to the configured page TTL. Renewal targets the
    CALLER's current page: a page this stack never created has no TTL slot
    and no renewal path, so the user's tabs are out of scope by construction.
    Runs under the serial lock, so a renewal and the expiry sweep can never
    interleave — a successful renewal proves the deadline had not passed.
    """
    unknown = set(args) - {"ttl"}
    if unknown:
        return _tool_error(f"renew_page got unexpected argument(s): {sorted(unknown)}")
    raw = args.get("ttl")
    if raw is None:
        ttl = _default_page_ttl_seconds()
    elif (
        isinstance(raw, bool)
        or not isinstance(raw, (int, float))
        or not math.isfinite(raw)
        or raw <= 0
    ):
        return _tool_error("ttl must be a finite number of seconds greater than zero")
    else:
        ttl = float(raw)
    if ttl > _MAX_TTL_SECONDS:
        return _tool_error(f"ttl must be at most {_MAX_TTL_SECONDS:.0f} seconds (24 hours)")
    async with daemon._lock:
        touch_agent_page(agent_id)
        page_id = _AGENT_AFFINITY.get(agent_id)
        if page_id is None:
            return _tool_error("this agent has no current page to renew; open one first")
        deadline = _PAGE_TTL_DEADLINES.get(page_id)
        if deadline is None:
            return _tool_error(
                f"page {page_id} has no page TTL to renew (not created through this "
                "browser, or already closed); open a new page"
            )
        now = time.monotonic()
        if now >= deadline:
            return _tool_error(
                f"page {page_id} is past its TTL deadline and cannot be renewed; open a new page"
            )
        _PAGE_TTL_DEADLINES[page_id] = now + ttl
        new_expires = datetime.now(UTC) + timedelta(seconds=ttl)
        telemetry.emit(
            "telemetry",
            "chrome_page_ttl_renewed",
            level="info",
            agent_id=agent_id,
            attributes={
                "page_id": page_id,
                "ttl_s": ttl,
                "new_expires_at": new_expires.isoformat(),
            },
        )
        logger.info(f"[browser-mcp] agent {agent_id} renewed page {page_id} TTL to {ttl:g}s")
        return _tool_ok(
            f"Page {page_id} TTL renewed: extends to {new_expires.isoformat()} ({ttl:g}s from now)."
        )


async def reap_idle_agent_pages(daemon: _PageDaemon) -> int:
    """Close agent-owned pages whose owner has been idle past the timeout.

    The affinity registry is the only candidate source, exactly like the
    dead-page sweep: the user's tabs and other agents' tabs are never
    inspected. Idle means the agent's last ``touch_agent_page`` is older
    than ``_TAB_IDLE_TIMEOUT_S`` — a terminated agent's stamp stops moving,
    so its tab (to any URL) ages out here even when nothing localhost-dead
    is involved. A live agent that keeps using the browser is never a
    candidate. A slot without a stamp (should not happen — every affinity
    page is created through an agent-keyed call) is treated as just-touched:
    the sweep never closes a page it knows nothing about. The candidate
    phase and the closes both run under the daemon's serial lock, and the
    page list is re-read before closing: a page that moved (re-purposed or
    closed) mid-pass is skipped.
    """
    now = time.monotonic()
    async with daemon._lock:
        listing = await daemon._call("list_pages", {})
        if listing.is_error:
            return 0
        page_urls = parse_page_listing(_text_of(listing))
        candidates: list[tuple[int, int]] = [
            (agent_id, page_id)
            for agent_id, page_id in list(_AGENT_AFFINITY.items())
            if page_id is not None
            and now - _AGENT_LAST_USE.get(agent_id, now) > _TAB_IDLE_TIMEOUT_S
        ]
        if not candidates:
            return 0
        closed = 0
        for agent_id, page_id in candidates:
            if page_urls.get(page_id) is None:
                # already closed upstream; drop the stale stamp alongside the slot
                _AGENT_LAST_USE.pop(agent_id, None)
                drop_page_ttl(page_id)
                continue
            result = await daemon._call("close_page", {"pageId": page_id})
            if result.is_error:
                logger.warning(
                    f"[browser-mcp] idle sweep could not close page {page_id} "
                    f"for agent {agent_id}: {_text_of(result)!r}"
                )
                continue
            if _AGENT_AFFINITY.get(agent_id) == page_id:
                _AGENT_AFFINITY[agent_id] = None
            _AGENT_LAST_USE.pop(agent_id, None)
            drop_page_ttl(page_id)
            closed += 1
            logger.info(f"[browser-mcp] idle sweep closed page {page_id} for agent {agent_id}")
        return closed


async def reap_expired_pages(daemon: _PageDaemon) -> int:
    """Close stack-created pages whose TTL deadline has passed.

    Candidate source is ``_PAGE_TTL_DEADLINES`` alone — only pages this stack
    created have a slot, so the user's tabs are never candidates. The whole
    pass runs under the serial lock (like the idle sweep): the candidate
    read, the page-list read, and the closes cannot interleave with a
    renewal or another close, so a renewed page is never closed off a stale
    read, and a close racing a concurrent ``close_page`` / release loses
    cleanly (the page is simply absent from the re-read listing). For a page
    still listed, the close re-checks nothing else: the deadline is the
    whole contract. After the close — whatever it said — the TTL slot and
    any affinity slot naming the page are dropped; the deadline passed, the
    page is done for its owner (an errored close is warned about; the reaper
    contract is that the page is dead either way). Expiry is surfaced to the
    agent only by the page being gone: the next page-scoped call hits the
    daemon's existing no-page path — no invented "page expired" error.
    Returns the number of pages closed.
    """
    async with daemon._lock:
        now = time.monotonic()
        expired = [
            page_id for page_id, deadline in list(_PAGE_TTL_DEADLINES.items()) if now >= deadline
        ]
        if not expired:
            return 0
        listing = await daemon._call("list_pages", {})
        if listing.is_error:
            return 0  # upstream hiccup — retry on the next pass
        page_urls = parse_page_listing(_text_of(listing))
        closed = 0
        for page_id in expired:
            url = page_urls.get(page_id)
            if url is None:
                # Already closed underneath us (close_page / release / a
                # manual tab close) — nothing to close; just drop the slot.
                drop_page_ttl(page_id)
                continue
            result = await daemon._call("close_page", {"pageId": page_id})
            if result.is_error:
                logger.warning(
                    f"[browser-mcp] TTL sweep could not close page {page_id} ({url}): "
                    f"{_text_of(result)!r}"
                )
            drop_page_ttl(page_id)
            owner = _clear_affinity_for_page(page_id)
            telemetry.emit(
                "log",
                "chrome_page_ttl_expired",
                level="info",
                agent_id=owner,
                attributes={"page_id": page_id, "url": url, "agent_id": owner},
            )
            closed += 1
            logger.info(
                f"[browser-mcp] TTL sweep closed page {page_id} ({url})"
                + (f" for agent {owner}" if owner is not None else "")
            )
        return closed


async def dead_page_reaper(daemon: _PageDaemon, stop: asyncio.Event) -> None:
    """Periodically sweep agent-owned pages: dead localhost pages (see
    ``reap_dead_agent_pages``), idle-owned pages whose owner has gone quiet
    (see ``reap_idle_agent_pages``), and pages past their hard TTL deadline
    (see ``reap_expired_pages`` — up to one sweep interval of lag on the
    deadline). Runs per upstream connection like the daemon's watchdog; a
    pass that hits upstream death returns so the daemon reconnects.
    """
    while not stop.is_set():
        with suppress(asyncio.TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=_DEAD_PAGE_SWEEP_INTERVAL_S)
        if stop.is_set():
            return
        try:
            await reap_dead_agent_pages(daemon)
            await reap_idle_agent_pages(daemon)
            await reap_expired_pages(daemon)
        except RuntimeError:
            # Upstream died mid-pass — daemon.dead is set; the daemon reconnects
            # and recreates this task on the new connection.
            return
        except Exception as e:
            # One bad pass must not kill the reaper.
            logger.warning(f"[browser-mcp] dead-page reaper pass failed: {e!r}")
