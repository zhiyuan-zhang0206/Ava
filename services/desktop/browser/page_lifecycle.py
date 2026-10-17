"""Agent-owned page lifecycle: release on terminate + TTL sweep.

The browser-mcp daemon (``services.desktop.browser.mcp_daemon``) keys one Chrome page
per agent (``PageRegistry.affinity``). A worker agent opens a tab on its first
navigate; when the agent terminates it must not leave that tab (usually a dev
server pointing at a dead localhost port) in the user's shared Chrome. Two
mechanisms, both scoped to agent-owned pages only:

- ``release_agent_page`` — the deterministic close: the agent's process-exit
  hook sends a ``release_agent_page`` wire request and the daemon closes that
  agent's page immediately (wire handler ``handle_release_agent_page``).
- ``expired_page_reaper`` / ``reap_expired_pages`` — the hard deadline and the
  only safety net (2026-09-11 ruling): every page this stack CREATED (an
  explicit new_page, or the auto-created page on a page-less first navigate) is
  registered in ``PageRegistry.ttl_deadlines`` with a deadline of
  ``now + MEIVA_CHROME_PAGE_DEFAULT_TTL_SECONDS`` (24h default), and the sweep
  closes it once that deadline passes. Activity never extends the deadline;
  ``renew_agent_page`` (the ``renew_page`` tool) moves it to ``now + ttl``
  explicitly, at most 24h per call. It bounds a page an agent keeps USING
  across days, and it is the only cleanup for the deaths that never reach the
  exit hook (SIGKILL / OOM / force-terminate) or a dev server that died under
  a live agent: that tab stays in the shared Chrome until its deadline.

Affinity records selection, including tabs the agent did not create. The
TTL record also proves creation: automatic cleanup requires that record even
when a tab has affinity. A user tab the agent merely selected has no TTL slot
and no renewal or automatic-close path.
The registries live in one ``PageRegistry`` the daemon's composition root
(``mcp_daemon.run``) builds for the daemon. The daemon serves exactly one
upstream connection; page ids are only meaningful within that process
(chrome-devtools-mcp numbers pages per process), and an upstream death ends the
daemon, so the registry never outlives the page numbering it records.
"""

from __future__ import annotations

import asyncio
import math
import re
import time
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from mcp import types

from base import telemetry
from base.config import settings
from base.log import logger
from services.desktop.browser.protocol import Request, Response

# Expiry sweep cadence: a page is closed on the first pass after its TTL
# deadline, so it can outlive the deadline by up to one interval.
PAGE_SWEEP_INTERVAL_S = 600.0

# Hard cap on one renewal grant — at most 24h, the same cap persistent shell
# sessions live under (user ruling 2026-09-01). The initial (creation) deadline
# is the configured default, `chrome_page_default_ttl_seconds`.
_MAX_TTL_SECONDS = 86_400.0

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


# Page-list line shapes, mirroring chrome-devtools-mcp's renderer: an untitled
# page is `  <id>: <url>`, a titled one `  <id>: <title> (<url>)`; either may
# end in ` [selected]` and zero or more ` key=value` suffixes (an isolated-
# context tab appends ` isolatedContext=<name>`). The URL is the bare second
# token, or the last `(…)` group of a titled label -- a URL that itself
# contains parens stays whole for the untitled shape.
_PAGE_LINE_RE = re.compile(
    r"^\s*(\d+):\s*(?:.+\s\((\S+)\)|(\S+))(?:\s+\[selected\])?(?:\s+\S+=\S+)*\s*$"
)


class PageRegistry:
    """The page-keyed state of the shared browser-mcp daemon, one per process.

    Built once by the daemon's composition root and handed to its
    ``ChromeMcpDaemon``. The daemon serves one upstream connection and exits
    when it dies, so every page id here belongs to the upstream process that is
    still serving.

    - ``affinity``: agent id -> current page id. The selected-page state
      belongs to the AGENT, not to a TCP connection: an exec subprocess child
      re-connecting mid-turn (or the agent process itself after a session
      rebuild) must land on the same tab the agent selected, not cold-start
      with "No page selected" on every exec. One entry per agent that has used
      the browser — bounded by the machine's agent count; a closed/crashed page
      drops the slot naturally via the existing re-pin failure path. Requests
      without an agent id (legacy wrapper clients) keep the per-connection
      fallback in the daemon.
    - ``ttl_deadlines``: page id -> monotonic deadline. Every page THIS STACK
      created (an explicit new_page call, or the auto-created page on a
      page-less first navigate) gets a slot from ``register_created_page``; the
      expiry sweep closes a page once its deadline passes. Monotonic (never
      wall clock): a clock step must not move a deadline. It is the ONLY
      candidate source for the TTL sweep, so pages this stack never created
      (the user's own tabs) are never inspected or closed.
    - ``page_owners``: page id -> creating agent id. Records which agent
      created each stack-created page so ``release_agent_page`` closes every
      page the agent opened (not only its last-selected tab) and
      ``reap_expired_pages`` attributes expired pages even after the agent
      moved on to another tab.
    """

    def __init__(self) -> None:
        self.affinity: dict[int, int | None] = {}
        self.ttl_deadlines: dict[int, float] = {}
        self.page_owners: dict[int, int] = {}

    def get_agent_page(self, agent_id: int) -> int | None:
        """The agent's current page, or None when it has none."""
        return self.affinity.get(agent_id)

    def set_agent_page(self, agent_id: int, page_id: int | None) -> None:
        """Record the agent's current page."""
        self.affinity[agent_id] = page_id

    def register_created_page(self, page_id: int | None, agent_id: int | None = None) -> None:
        """Give a freshly created page its initial TTL deadline and record its owner.

        Called by the daemon right after a ``new_page`` it forwarded (an
        explicit call, or the auto-created page on a page-less first navigate)
        resolved to a page id. Best-effort like the affinity parse it rides on:
        a listing that drifted off the parseable shape yields ``None`` and the
        page simply carries no TTL. Callers hold the daemon's serial lock.
        """
        if page_id is None:
            return
        self.ttl_deadlines[page_id] = time.monotonic() + _default_page_ttl_seconds()
        if agent_id is not None:
            self.page_owners[page_id] = agent_id

    def is_created_page(self, page_id: int) -> bool:
        """Whether this stack created the page, even after its TTL elapsed."""
        return page_id in self.ttl_deadlines

    def agent_created_pages(self, agent_id: int) -> list[int]:
        """Every stack-created page owned by ``agent_id``, in creation order."""
        owned = [pid for pid in self.ttl_deadlines if self.page_owners.get(pid) == agent_id]
        current = self.get_agent_page(agent_id)
        if (
            current is not None
            and current not in owned
            and self.is_created_page(current)
            and self.page_owners.get(current) is None
        ):
            owned.append(current)
        return owned

    def drop_page_ttl(self, page_id: int | None) -> None:
        """Forget the page's TTL and owner slots — the page is closed (or was never ours)."""
        if page_id is not None:
            self.ttl_deadlines.pop(page_id, None)
            self.page_owners.pop(page_id, None)

    def clear_affinity_for_page(self, page_id: int) -> int | None:
        """Clear every agent slot naming ``page_id`` (the page is gone);

        returns the creating owner (or one cleared affinity owner) for logs /
        the expiry event (None when no slot named the page). Every slot is
        cleared — a slot left naming a closed page would make the owner's next
        call re-pin against a dead id first.
        """
        owner: int | None = self.page_owners.pop(page_id, None)
        for agent_id, current in list(self.affinity.items()):
            if current == page_id:
                self.set_agent_page(agent_id, None)
                if owner is None:
                    owner = agent_id
        return owner


class _PageDaemon(Protocol):
    """The slice of ``ChromeMcpDaemon`` the page-lifecycle helpers use.

    A structural protocol keeps this module free of a circular import with the
    daemon (which imports this module for the reaper + wire handling) while
    staying pyright-clean.
    """

    _lock: asyncio.Lock
    pages: PageRegistry

    async def _call(self, name: str, args: dict[str, Any]) -> types.CallToolResult: ...

    async def call_tool(
        self, name: str, args: dict[str, Any], current_page: int | None
    ) -> tuple[types.CallToolResult, int | None]: ...


def text_of(result: types.CallToolResult) -> str:
    return "".join(c.text for c in result.content if isinstance(c, types.TextContent))


async def forward_legacy_call(
    daemon: _PageDaemon,
    tool: str,
    args: dict[str, Any],
    conn_page: int | None,
) -> tuple[types.CallToolResult, int | None]:
    """Forward one agent-less call with the connection's own page.

    Returns the result and the connection's updated current page.
    """
    return await daemon.call_tool(tool, args, conn_page)


async def release_agent_page(daemon: _PageDaemon, agent_id: int) -> int | None:
    """Drop affinity, closing every page this agent created.

    Called when the agent terminates (wire method ``release_agent_page``,
    sent by the termination cleanup hook). Idempotent: an agent with no
    created pages or affinity is a no-op. A borrowed page is deselected
    without closing; every created page owned by ``agent_id`` is closed so the
    agent's tab group disappears with its last tab. The returned id is the
    closed current page (or the last closed created page), or None.
    """
    async with daemon._lock:
        current_id = daemon.pages.get_agent_page(agent_id)
        to_close = daemon.pages.agent_created_pages(agent_id)
        if not to_close:
            if current_id is not None:
                daemon.pages.set_agent_page(agent_id, None)
            return None
        last_closed: int | None = None
        for page_id in to_close:
            result = await daemon._call("close_page", {"pageId": page_id})
            # A close of an already-gone page errors upstream; either way the slot
            # is released — this agent is done with the browser. Only a transport
            # failure (upstream down) aborts the release, and then the exception
            # propagates and the remaining slots survive for the expiry sweep.
            if result.is_error:
                logger.warning(
                    f"[browser-mcp] release of agent {agent_id} page {page_id} "
                    f"errored ({text_of(result)!r}); clearing the slot"
                )
            daemon.pages.clear_affinity_for_page(page_id)
            daemon.pages.drop_page_ttl(page_id)
            last_closed = page_id
        daemon.pages.set_agent_page(agent_id, None)
        return current_id if current_id in to_close else last_closed


async def handle_release_agent_page(daemon: _PageDaemon, req: Request, req_id: Any) -> Response:
    """Release affinity, closing created pages, on a ``release_agent_page`` request.

    The request is a terminated agent's exit hook telling the service it is
    done with the browser; the reply names the page that was closed (None for
    a borrowed page or no affinity). A missing/aliased agent id is rejected at the protocol
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
            # Titled label -> the `(…)` group; untitled -> the bare token.
            pages[int(m.group(1))] = m.group(2) or m.group(3)
    return pages


def _default_page_ttl_seconds() -> float:
    """The configured default TTL for a newly created page."""
    return float(settings.daemon.chrome_page_default_ttl_seconds)


def _tool_ok(text: str) -> types.CallToolResult:
    return types.CallToolResult(content=[types.TextContent(type="text", text=text)], is_error=False)


def _tool_error(text: str) -> types.CallToolResult:
    return types.CallToolResult(content=[types.TextContent(type="text", text=text)], is_error=True)


async def renew_agent_page(
    daemon: _PageDaemon, agent_id: int, args: dict[str, Any]
) -> types.CallToolResult:
    """Handle one ``renew_page`` tool call: move the agent page's TTL deadline.

    Semantics mirror ``meiva.shell.sessions.renew`` (the shell-TTL ruling): the
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
        page_id = daemon.pages.get_agent_page(agent_id)
        if page_id is None:
            return _tool_error("this agent has no current page to renew; open one first")
        deadline = daemon.pages.ttl_deadlines.get(page_id)
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
        daemon.pages.ttl_deadlines[page_id] = now + ttl
        new_expires = datetime.now(UTC) + timedelta(seconds=ttl)
        telemetry.emit(
            "telemetry",
            "chrome_page_ttl_renewed",
            level="info",
            agent_id=agent_id,
            attributes={
                "page_id": page_id,
                # Same-class cast as task #4011 (stable emitted metric kind).
                "ttl_s": round(ttl),
                "new_expires_at": new_expires.isoformat(),
            },
        )
        logger.info(f"[browser-mcp] agent {agent_id} renewed page {page_id} TTL to {ttl:g}s")
        return _tool_ok(
            f"Page {page_id} TTL renewed: extends to {new_expires.isoformat()} ({ttl:g}s from now)."
        )


async def reap_expired_pages(daemon: _PageDaemon) -> int:
    """Close stack-created pages whose TTL deadline has passed.

    Candidate source is ``PageRegistry.ttl_deadlines`` alone — only pages this stack
    created have a slot, so the user's tabs are never candidates. The whole
    pass runs under the serial lock: the candidate
    read, the page-list read, and the closes cannot interleave with a
    renewal or another close, so a renewed page is never closed off a stale
    read, and a close racing a concurrent ``close_page`` / release loses
    cleanly (the page is simply absent from the re-read listing). For a page
    still listed, the close re-checks nothing else: the deadline is the
    whole contract. After the close — whatever it said — the TTL slot and
    any affinity slot naming the page are dropped. The deadline is terminal,
    the same doctrine as the shell-TTL ruling's "no renewable expired
    state": a page already closed elsewhere is filtered by the listing
    re-check before any close attempt, so an errored close from here is
    anomalous — warned about, never retried (a retryable slot would keep a
    terminal deadline alive past its deadline). Expiry is surfaced to the
    agent only by the page being gone: the next page-scoped call hits the
    daemon's existing no-page path — no invented "page expired" error.
    Returns the number of pages closed.
    """
    async with daemon._lock:
        now = time.monotonic()
        expired: list[int] = []
        for page_id, deadline in list(daemon.pages.ttl_deadlines.items()):
            if now >= deadline:
                expired.append(page_id)
        if not expired:
            return 0
        listing = await daemon._call("list_pages", {})
        if listing.is_error:
            return 0  # upstream hiccup — retry on the next pass
        page_urls = parse_page_listing(text_of(listing))
        closed = 0
        for page_id in expired:
            url = page_urls.get(page_id)
            if url is None:
                # Already closed underneath us (close_page / release / a
                # manual tab close) — nothing to close; just drop the slot.
                daemon.pages.clear_affinity_for_page(page_id)
                daemon.pages.drop_page_ttl(page_id)
                continue
            result = await daemon._call("close_page", {"pageId": page_id})
            if result.is_error:
                logger.warning(
                    f"[browser-mcp] TTL sweep could not close page {page_id} ({url}): "
                    f"{text_of(result)!r}"
                )
            owner = daemon.pages.clear_affinity_for_page(page_id)
            daemon.pages.drop_page_ttl(page_id)
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


async def expired_page_reaper(daemon: _PageDaemon, stop: asyncio.Event) -> None:
    """Periodically close pages past their hard TTL deadline (see
    ``reap_expired_pages`` — up to one sweep interval of lag on the deadline).
    Runs for the daemon's upstream connection like its watchdog; a pass that
    hits upstream death returns so the daemon exits.
    """
    # quiesce-exempt: reaps expired browser pages; no database
    while not stop.is_set():
        with suppress(asyncio.TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=PAGE_SWEEP_INTERVAL_S)
        if stop.is_set():
            return
        try:
            await reap_expired_pages(daemon)
        except RuntimeError:
            # Upstream died mid-pass — daemon.dead is set; the daemon exits.
            return
        except Exception as e:
            # One bad pass must not kill the reaper.
            logger.warning(f"[browser-mcp] expired-page reaper pass failed: {e!r}")
