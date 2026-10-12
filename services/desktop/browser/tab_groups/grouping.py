"""One Chrome tab group per agent, for the pages that agent's browser calls create.

Chrome's DevTools protocol has no tab-group API (measured on Chrome 153: none
of its domains mentions groups), so grouping goes through a small extension
shipped in ``extension/``. The browser-mcp daemon loads it with
``Extensions.loadUnpacked`` -- available on the ordinary
``--remote-debugging-port`` connection, no launch flag -- and calls the
``avaGroup(targetId, title)`` function its service worker exposes.

Three facts shape the design:

- **Loading is per Chrome session and the worker idles out.** An unpacked
  extension loaded over CDP is gone after a Chrome restart, and its service
  worker stops ~30s after its last event (its CDP target disappears). Each
  grouping therefore checks, on the same websocket, whether the extension is
  loaded with a live worker of the file name this checkout ships, and issues
  the idempotent ``Extensions.loadUnpacked`` only when it is not (Chrome
  restarted, worker idled out, or an older worker file is still running). It
  must not reload a live worker: the reload tears the worker down, and a
  call that attached to the dying target never gets its reply (measured).
  After attaching it waits for the worker script to have defined ``avaGroup``
  (the target can be attachable first). Nothing is remembered between calls,
  so daemon restarts need no repair.
- **Page ids are not target ids.** The daemon's page ids are chrome-devtools-mcp
  sequence numbers; the extension needs the CDP targetId. The caller holds the
  daemon's machine-wide serial lock, so the one page target that appears
  between ``snapshot()`` (before ``new_page``) and ``group_new_page()`` (after)
  is the page that was just created. Anything other than exactly one new
  target (a user opening a tab meanwhile, a popup) is reported and left
  ungrouped -- the module never guesses which tab is the agent's.
- **Only the two target listings run under that lock.** They are short
  (``_LISTING_TIMEOUT_S``). Everything else -- loading the extension, attaching
  to its worker, calling it -- runs in a background task with its own timeout
  (``_GROUP_TIMEOUT_S``), so a slow or wedged Chrome costs the page creation
  nothing. The tasks run one at a time (``_lock``, the grouping's own lock, not
  the daemon's): two grouping tasks of one agent both search the group by title
  and would otherwise each create it. ``cancel_pending()`` drops in-flight
  tasks when the daemon stops (an upstream death ends the process).
- **Nothing here deletes a group.** Chrome removes a tab group, and its saved
  group record, when the group's last tab closes. Every way this stack closes
  an agent's page goes through ``close_page`` (``page_lifecycle``), so an
  agent's group disappears with its last page.

Grouping is presentation only. Every failure -- extension refused, CDP error, no
unique new target, a wedged Chrome -- is logged as a WARNING and swallowed: the
browser operation it decorates must succeed or fail on its own merits (see
docs/decisions/engineering/design/contracts/2026-10-12-presentation-only-failure-degrades-with-warning.md).
"""

from __future__ import annotations

import asyncio
import itertools
import json
from pathlib import Path
from typing import Any

import websockets

from base.log import logger
from services.desktop.browser.session import _cdp_call, _http_json

# The unpacked extension Chrome loads. Its service-worker file name carries a
# hash of its content: Chrome caches an extension's worker script in the
# profile and keeps running the cached copy when only the content changes, so
# only a new file name reliably ships new code (a test enforces the name).
EXTENSION_DIR = Path(__file__).resolve().parent / "extension"

# Bound on each of the two target listings. The caller holds the daemon's serial
# lock around them, so a wedged Chrome must not stall every agent's browser call
# for longer than this; a local /json/list answers in milliseconds.
_LISTING_TIMEOUT_S = 2.0

# Bound on one background grouping (load, attach, call), excluding the wait for
# the grouping lock. It holds no daemon lock.
_GROUP_TIMEOUT_S = 10.0

# How long to wait for the extension's service worker target to appear after
# `loadUnpacked` (it normally is up within milliseconds).
_WORKER_WAIT_S = 5.0
_WORKER_POLL_S = 0.1


def group_title(agent_id: int) -> str:
    """The tab group title of an agent: ``Ava #<id>``.

    The title is the group's identity (the worker finds a group by title), so it
    names the agent by id -- labels can repeat, ids cannot. It carries no TTL and
    no color; Chrome picks the color.
    """
    return f"Ava #{agent_id}"


class _NoUniqueNewPageError(RuntimeError):
    """The page targets did not differ by exactly one between snapshot and grouping."""


async def _page_target_ids(cdp_port: int) -> frozenset[str]:
    listing = await _http_json(f"http://127.0.0.1:{cdp_port}/json/list")
    return frozenset(t["id"] for t in listing if t.get("type") == "page")


def _worker_file() -> str:
    """The service-worker file name the shipped manifest declares."""
    manifest = json.loads((EXTENSION_DIR / "manifest.json").read_text())
    return manifest["background"]["service_worker"]


async def _live_worker(ws: Any, ids: Any, extension_id: str) -> str | None:
    """The target id of this checkout's worker file running in ``extension_id``, if any."""
    url = f"chrome-extension://{extension_id}/{_worker_file()}"
    infos = (await _cdp_call(ws, ids, "Target.getTargets", {}))["targetInfos"]
    return next(
        (i["targetId"] for i in infos if i["type"] == "service_worker" and i["url"] == url), None
    )


async def _worker_session(ws: Any, ids: Any) -> str:
    """Attach to the extension's service worker, loading the extension if needed.

    Returns the flattened session id. Reuses a live worker of the shipped file
    name; otherwise (not loaded, worker idled out, or an older worker file)
    issues ``Extensions.loadUnpacked`` and waits for the worker to come up.
    """
    loaded = [
        e
        for e in (await _cdp_call(ws, ids, "Extensions.getExtensions", {}))["extensions"]
        if Path(e["path"]) == EXTENSION_DIR
    ]
    target_id = await _live_worker(ws, ids, loaded[0]["id"]) if loaded else None
    if target_id is None:
        loaded_id = (
            await _cdp_call(ws, ids, "Extensions.loadUnpacked", {"path": str(EXTENSION_DIR)})
        )["id"]
        loop = asyncio.get_running_loop()
        deadline = loop.time() + _WORKER_WAIT_S
        while (target_id := await _live_worker(ws, ids, loaded_id)) is None:
            if loop.time() >= deadline:
                raise RuntimeError(f"extension {loaded_id} service worker target not found")
            await asyncio.sleep(_WORKER_POLL_S)
    attached = await _cdp_call(
        ws, ids, "Target.attachToTarget", {"targetId": target_id, "flatten": True}
    )
    session_id = attached["sessionId"]
    await _await_worker_script(ws, ids, session_id)
    return session_id


async def _await_worker_script(ws: Any, ids: Any, session_id: str) -> None:
    """Wait until the worker's script has defined ``avaGroup``.

    The worker's CDP target can be attachable a moment before its script has
    run (measured on first load: ``avaGroup is not defined``).
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + _WORKER_WAIT_S
    # quiesce-exempt: bounded poll (_WORKER_WAIT_S) of a local browser; no database
    while True:
        probe = await _cdp_call(
            ws,
            ids,
            "Runtime.evaluate",
            {"expression": "typeof avaGroup", "returnByValue": True},
            session_id=session_id,
        )
        if probe["result"].get("value") == "function":
            return
        if loop.time() >= deadline:
            raise RuntimeError("extension worker did not define avaGroup")
        await asyncio.sleep(_WORKER_POLL_S)


class AgentTabGroups:
    """Files agent-created pages into per-agent tab groups. Stateless and create-only.

    Built once by the daemon's composition root (with its root ``TaskGroup``, which
    owns the background tasks) and shared by every ``ChromeMcpDaemon`` it
    creates. Callers hold the daemon's serial lock
    around ``snapshot`` -> ``new_page`` -> ``group_new_page``; ``group_new_page``
    returns after one short target listing and finishes the grouping in a
    background task. Neither method raises, and every failure is a WARNING.
    """

    def __init__(self, cdp_port: int, tasks: asyncio.TaskGroup) -> None:
        self._cdp_port = cdp_port
        self._task_group = tasks  # the daemon's root group: it owns every task's lifetime
        self._lock = asyncio.Lock()  # serializes groupings only; the daemon's lock is not held
        self._tasks: set[asyncio.Task[None]] = set()

    async def snapshot(self) -> frozenset[str] | None:
        """The ids of Chrome's page targets right now, taken before a ``new_page``.

        None when they could not be listed (already reported): the new page then
        stays ungrouped.
        """
        try:
            return await asyncio.wait_for(_page_target_ids(self._cdp_port), _LISTING_TIMEOUT_S)
        except Exception as e:
            logger.opt(exception=True).warning(
                "[browser-mcp] could not list Chrome page targets for tab grouping "
                "({}: {}); the next new page stays ungrouped",
                type(e).__name__,
                e,
            )
            return None

    async def group_new_page(self, agent_id: int, before: frozenset[str] | None) -> None:
        """Start filing the page created since ``before`` into the agent's tab group.

        ``before`` is the matching ``snapshot()``; None (its failure was
        reported) skips. Finds the new page's target with one short listing,
        then hands the grouping to a background task and returns: the page is
        grouped moments later, not before this returns.
        """
        if before is None:
            return
        try:
            target_id = await asyncio.wait_for(self._new_target(before), _LISTING_TIMEOUT_S)
        except Exception as e:
            self._warn(agent_id, e)
            return
        task = self._task_group.create_task(
            self._file(agent_id, target_id), name=f"tab-group-agent-{agent_id}"
        )
        self._tasks.add(task)
        task.add_done_callback(self._settled)

    async def _new_target(self, before: frozenset[str]) -> str:
        new = (await _page_target_ids(self._cdp_port)) - before
        if len(new) != 1:
            raise _NoUniqueNewPageError(
                f"expected exactly one new page target, saw {len(new)} new page targets"
            )
        return next(iter(new))

    def cancel_pending(self) -> None:
        """Drop every in-flight grouping (daemon stop, which an upstream death also causes).

        A dropped grouping leaves its page ungrouped and reports nothing: the
        reason is the daemon's own stop.
        """
        for task in self._tasks:
            task.cancel()

    def _settled(self, task: asyncio.Task[None]) -> None:
        self._tasks.discard(task)
        if not task.cancelled():
            task.exception()  # `_file` reports its own failures; this marks it retrieved

    async def _file(self, agent_id: int, target_id: str) -> None:
        title = group_title(agent_id)
        try:
            async with self._lock:
                await asyncio.wait_for(self._apply(title, target_id), _GROUP_TIMEOUT_S)
        except Exception as e:
            self._warn(agent_id, e)

    @staticmethod
    def _warn(agent_id: int, error: Exception) -> None:
        logger.opt(exception=not isinstance(error, _NoUniqueNewPageError)).warning(
            "[browser-mcp] could not file agent {}'s new page into tab group {!r} "
            "({}: {}); the page is still open and usable",
            agent_id,
            group_title(agent_id),
            type(error).__name__,
            error,
        )

    async def _apply(self, title: str, target_id: str) -> None:
        version = await _http_json(f"http://127.0.0.1:{self._cdp_port}/json/version")
        ids = itertools.count(1)
        # Closing the websocket detaches the worker session opened below.
        async with websockets.connect(version["webSocketDebuggerUrl"]) as ws:
            session_id = await _worker_session(ws, ids)
            outcome = await _cdp_call(
                ws,
                ids,
                "Runtime.evaluate",
                {
                    "expression": f"avaGroup({json.dumps(target_id)}, {json.dumps(title)})",
                    "awaitPromise": True,
                    "returnByValue": True,
                },
                session_id=session_id,
            )
        if "exceptionDetails" in outcome:
            raise RuntimeError(outcome["exceptionDetails"]["exception"]["description"])
