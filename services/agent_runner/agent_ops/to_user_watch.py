"""One shallow filesystem watcher for every local agent's user context.

The watch itself runs on this module's own thread, never the daemon's event
loop. Constructing the watcher is the expensive half — measured on a runner of
~3700 workspaces at seconds for hundreds of watched paths and past two minutes
at that count — and every workspace-root change rebuilds it. Run on the loop,
one rebuild owned the process for minutes: `/healthz` timed out and a normal
stop's SIGTERM went unhandled (2026-10-14, both ops units of one wave; task
#5129). The loop here only receives finished batches and publishes them, so no
construction, rebuild or teardown can starve it.

The thread is owned and daemon, with a bounded stop: a stopping daemon sets its
`threading.Event` and gives the join one second, then leaves a thread that is
still inside a construction behind — the process exits through `hard_exit`,
which skips thread joins, and waiting on this thread is what the loop side must
never do.
"""

from __future__ import annotations

import asyncio
import logging
import math
import threading
from collections.abc import Callable, Iterator
from pathlib import Path

from watchfiles import Change, watch

from base.events.live.bus import EventBus
from base.events.live.projection import ToUserChanged

_log = logging.getLogger("services.agent_runner.agent_ops.to_user_watch")

# How long one batch groups changes before it is yielded; taken from the async
# form this replaced, where watchfiles applied the same default.
_DEBOUNCE_MS = 150
# How long the rust watcher waits for events before it re-checks the stop
# event. Much shorter than watchfiles' 5s default, so a cancelled watch ends
# well inside the unit's 10s stop window.
_STOP_CHECK_MS = 500
# A failed watch round is retried after this pause instead of ending the watch:
# the user context is a best-effort UI signal, and the usual failure — a
# workspace removed between the directory scan and the watcher's construction —
# is a race the next round repairs.
_RETRY_PAUSE_S = 1.0
# This thread's name in a stack dump; the ops runbook looks for stuck threads.
_THREAD_NAME = "ava-ops-to-user-watch"
# How long a stopped watcher is given to come out of its own setup before the
# daemon leaves it behind: a construction in flight can take minutes, and the
# loop must not wait on it.
_STOP_JOIN_S = 1.0

# The blocking watch one round is built on. An explicit parameter rather than a
# module lookup, so a caller supplies the source: the daemon leaves the default
# and tests pass a fake that can block on demand, which no real watcher over
# thousands of paths does to order.
WatchSource = Callable[..., Iterator[set[tuple[Change, str]]]]


def changed_agent(root: Path, path: Path) -> tuple[int | None, bool]:
    """Map a shallow watch event to an agent and whether watch roots changed."""
    if path.parent == root and path.name.isdecimal():
        return int(path.name), True
    if (
        path.name in {"to_user.html", "to_user.md"}
        and path.parent.parent == root
        and path.parent.name.isdecimal()
    ):
        return int(path.parent.name), False
    return None, False


def _workspace_roots(root: Path) -> list[Path]:
    """The watched paths below the workspace root, re-read for every watch round."""
    # Never recurse into repositories or node_modules inside workspaces.
    return [p for p in root.iterdir() if p.name.isdecimal() and p.is_dir() and not p.is_symlink()]


def _batches(root: Path, stop: threading.Event, source: WatchSource) -> Iterator[set[int]]:
    """Yield one changed-agent batch per watch round, until `stop` is set.

    A root change ends the round and rebuilds the watcher: a workspace appeared
    or disappeared, so the watched-path set moved. The batch that reveals it is
    yielded first, so the agent owning that workspace is announced before the
    watcher is replaced underneath it.
    """
    while not stop.is_set():
        for changes in source(
            root,
            *_workspace_roots(root),
            recursive=False,
            debounce=_DEBOUNCE_MS,
            stop_event=stop,
            rust_timeout=_STOP_CHECK_MS,
        ):
            changed_agents: set[int] = set()
            rebuild = False
            for change, raw_path in changes:
                agent_id, roots_changed = changed_agent(root, Path(raw_path))
                if roots_changed and change == Change.modified:
                    continue
                rebuild |= roots_changed
                if agent_id is not None:
                    changed_agents.add(agent_id)
            yield changed_agents
            if rebuild:
                break


def _report_worker_failure(exc: BaseException) -> None:
    """The watcher thread's one error observer: a failure there must never be silent."""
    _log.error("to_user watch worker failed: %r", exc, exc_info=exc)


class _ToUserWatch:
    """Own the one thread per workspace root, with admission, stop and a bounded join.

    The thread pays the watcher's construction, iteration and rebuilds; the
    owner records its failure and reports it as unfinished work when a stop
    cannot join it — an in-flight construction is not interruptible.
    """

    def __init__(
        self,
        root: Path,
        publish: Callable[[set[int]], None],
        *,
        watch_source: WatchSource = watch,
    ) -> None:
        self._root = root
        self._publish = publish
        self._watch_source = watch_source
        self._stop_requested = threading.Event()
        self.finished = threading.Event()
        self._error: BaseException | None = None
        self._thread = threading.Thread(target=self._run, daemon=True, name=_THREAD_NAME)
        self._thread.start()

    def _run(self) -> None:
        try:
            while not self._stop_requested.is_set():
                try:
                    for changed_agents in _batches(
                        self._root, self._stop_requested, self._watch_source
                    ):
                        self._publish(changed_agents)
                except Exception:
                    _log.exception("to_user watch round failed; re-establishing the watch")
                    self._stop_requested.wait(_RETRY_PAUSE_S)
        except BaseException as exc:
            self._error = exc
            _report_worker_failure(exc)
        finally:
            self.finished.set()

    def request_stop(self) -> None:
        self._stop_requested.set()

    def stop(self, timeout: float = _STOP_JOIN_S) -> bool:
        """Stop admission, then observe this thread finitely; False means still running."""
        if not math.isfinite(timeout) or timeout < 0:
            raise ValueError("watcher stop timeout must be finite and non-negative")
        self._stop_requested.set()
        self._thread.join(timeout=timeout)
        alive = self._thread.is_alive()
        if self._error is not None:
            raise self._error
        return not alive


async def watch_to_user(root: Path, bus: EventBus, *, watch_source: WatchSource = watch) -> None:
    root.mkdir(parents=True, exist_ok=True)
    loop = asyncio.get_running_loop()
    # Unreported agents, not a queue of batches: a slow publish merges the next
    # batch into this set instead of growing without bound, and every agent that
    # changed is still named in the batch that is published.
    pending: set[int] = set()
    ready = asyncio.Event()

    def offer(changed_agents: set[int]) -> None:
        pending.update(changed_agents)
        ready.set()

    def publish(changed_agents: set[int]) -> None:
        if not changed_agents:
            return
        try:
            loop.call_soon_threadsafe(offer, changed_agents)
        except RuntimeError:  # the loop is gone: nothing left to publish to
            return

    watcher = _ToUserWatch(root, publish, watch_source=watch_source)
    try:
        while True:
            await ready.wait()
            batch = sorted(pending)  # no await above: an offer cannot slip between these lines
            pending.clear()
            ready.clear()
            for agent_id in batch:
                await bus.publish_best_effort(
                    ToUserChanged(agent_id=agent_id).model_dump_json(), context="to_user"
                )
    finally:
        # Bounded join, off the loop: the watcher may be inside a construction
        # that takes minutes, and an unfinished thread is reported, never waited on.
        settled = await asyncio.to_thread(watcher.stop, _STOP_JOIN_S)
        if not settled:
            _log.warning(
                "to-user watcher did not come out of its setup within %ss; leaving the thread",
                _STOP_JOIN_S,
            )
