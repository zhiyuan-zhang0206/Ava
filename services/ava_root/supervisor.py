"""The supervise loop: spawn units, follow restart policy, keep the tree alive.

This is the platform-neutral core of the root supervisor. It owns the process
table of one tree:

- `up` / `down` / `restart` act on subtrees (a unit and its attach descendants);
- an unexpected exit follows the unit's restart policy, with exponential
  backoff for repeats;
- every child process is reaped through its own wait task — no orphaned exit
  status is left behind anywhere in the tree.

Chain discipline (I2) shows up here as what this code deliberately does *not*
do: units are spawned as plain children (no double-fork, no new session, no
detach), so a unit's parent is this process for its whole life. A manual
`restart` replaces a unit's generation start-new-before-stop-old — a failed
new start must not take the old instance down — and this process itself never
exits for a unit action.
"""

from __future__ import annotations

import asyncio
import logging
import os
from contextlib import suppress
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from time import monotonic

from services.ava_root.ipc import (
    ErrorCode,
    RequestPayload,
    ResponsePayload,
    Verb,
    error_response,
    ok_response,
)
from services.ava_root.manifest import (
    RestartPolicy,
    UnitManifest,
    UnitRegistry,
    UnknownUnitError,
)

_log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class SupervisorConfig:
    """Timing policy for the supervise loop (test-tunable)."""

    backoff_base_s: float = 1.0
    """First restart delay; doubles per consecutive failure up to `backoff_max_s`."""

    backoff_max_s: float = 30.0
    """Ceiling for the restart delay."""

    stable_after_s: float = 30.0
    """A generation that lived at least this long resets the failure streak."""

    stop_timeout_s: float = 10.0
    """Grace period between a polite stop request and the forceful kill."""


class UnitState(StrEnum):
    """What a unit's process is doing right now."""

    RUNNING = "running"
    STOPPED = "stopped"
    BACKOFF = "backoff"


class DesiredState(StrEnum):
    """What the supervisor has been told to make true for a unit."""

    RUNNING = "running"
    STOPPED = "stopped"


@dataclass(slots=True)
class _Generation:
    """One process instance of a unit.

    `exited` is set as the *last* action of the wait task, so any coroutine
    that observes the event also observes the exit fully processed.
    """

    proc: asyncio.subprocess.Process
    started_at: float
    exited: asyncio.Event = field(default_factory=asyncio.Event)


@dataclass(slots=True)
class _UnitRuntime:
    """Mutable per-unit state owned by the supervisor."""

    manifest: UnitManifest
    desired: DesiredState = DesiredState.STOPPED
    state: UnitState = UnitState.STOPPED
    generation: _Generation | None = None
    restart_count: int = 0
    failure_streak: int = 0
    last_exit: str | None = None
    last_error: str | None = None
    backoff_until: float | None = None
    watch_task: asyncio.Task[None] | None = None
    restart_task: asyncio.Task[None] | None = None


class Supervisor:
    """Owns the lifecycle of every unit in one registry.

    All mutating verbs serialize on one lock, so overlapping commands are
    ordered instead of racing; `status` is lock-free (a single event-loop turn
    reads a consistent snapshot).
    """

    def __init__(
        self,
        registry: UnitRegistry,
        *,
        log_dir: Path,
        config: SupervisorConfig | None = None,
    ) -> None:
        self._registry = registry
        self._log_dir = log_dir
        self._config = config if config is not None else SupervisorConfig()
        self._units: dict[str, _UnitRuntime] = {
            manifest.id: _UnitRuntime(manifest=manifest) for manifest in registry.units
        }
        self._lock = asyncio.Lock()
        self._started_at: float | None = None
        self._running = False

    # ── lifecycle ────────────────────────────────────────────────────────────

    async def start(self) -> None:
        """Bring up the whole tree: parallel bring-up, no dependency order."""
        self._log_dir.mkdir(parents=True, exist_ok=True)
        async with self._lock:
            self._running = True
            self._started_at = monotonic()
            for runtime in self._units.values():
                runtime.desired = DesiredState.RUNNING
            await asyncio.gather(*(self._start_unit(runtime) for runtime in self._units.values()))

    async def shutdown(self) -> None:
        """Stop the whole tree (children before parents) and drain the tasks."""
        async with self._lock:
            self._running = False
            for unit_id in self._registry.all_stop_order():
                runtime = self._units[unit_id]
                runtime.desired = DesiredState.STOPPED
                await self._stop_unit(runtime)
        pending = [
            task
            for runtime in self._units.values()
            for task in (runtime.watch_task, runtime.restart_task)
            if task is not None
        ]
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        for runtime in self._units.values():
            runtime.watch_task = None
            runtime.restart_task = None

    # ── control verbs ────────────────────────────────────────────────────────

    async def up(self, unit_id: str) -> dict[str, object]:
        """Ensure `unit_id` and its subtree are running; idempotent per unit."""
        subtree = self._registry.subtree(unit_id)
        async with self._lock:
            results: list[dict[str, object]] = []
            for member in subtree:
                runtime = self._units[member]
                runtime.desired = DesiredState.RUNNING
                await self._cancel_task(runtime.restart_task)
                runtime.restart_task = None
                if self._is_active(runtime):
                    action = "already-running"
                else:
                    await self._start_unit(runtime)
                    action = "started" if self._is_active(runtime) else "failed"
                results.append(self._unit_result(runtime, action))
        return {"verb": Verb.UP.value, "units": results}

    async def down(self, unit_id: str) -> dict[str, object]:
        """Stop `unit_id` and its subtree, children before parents."""
        order = self._registry.stop_order(unit_id)
        async with self._lock:
            results: list[dict[str, object]] = []
            for member in order:
                runtime = self._units[member]
                runtime.desired = DesiredState.STOPPED
                was_active = self._is_active(runtime)
                await self._stop_unit(runtime)
                action = "stopped" if was_active else "already-stopped"
                results.append(self._unit_result(runtime, action))
        return {"verb": Verb.DOWN.value, "units": results}

    async def restart(self, unit_id: str) -> dict[str, object]:
        """Roll `unit_id`'s subtree: start every fresh generation first, then
        stop the old ones (children before parents). A unit whose fresh start
        fails keeps its running old generation — that is the point of the
        ordering."""
        subtree = self._registry.subtree(unit_id)
        async with self._lock:
            results: list[dict[str, object]] = []
            replaced: list[tuple[_UnitRuntime, _Generation | None]] = []
            for member in subtree:
                runtime = self._units[member]
                runtime.desired = DesiredState.RUNNING
                await self._cancel_task(runtime.restart_task)
                runtime.restart_task = None
                old = runtime.generation
                try:
                    await self._spawn(runtime)
                except OSError as exc:
                    runtime.last_error = f"spawn failed: {exc}"
                    _log.error("unit %s: restart spawn failed: %s", member, exc)
                    if old is None:
                        runtime.state = UnitState.STOPPED
                        self._maybe_schedule_restart(runtime)
                    results.append(self._unit_result(runtime, "failed"))
                    continue
                if old is not None:
                    runtime.restart_count += 1
                replaced.append((runtime, old))
                result = self._unit_result(runtime, "replaced" if old is not None else "started")
                results.append(result)
            for runtime, old in reversed(replaced):
                if old is not None:
                    await self._stop_generation(runtime, old)
        return {"verb": Verb.RESTART.value, "units": results}

    async def status(self) -> dict[str, object]:
        """The tree snapshot: structure, health, and restart counters."""
        units: list[dict[str, object]] = []
        restarts_total = 0
        for runtime in self._units.values():
            restarts_total += runtime.restart_count
            entry: dict[str, object] = {
                "id": runtime.manifest.id,
                "attach": runtime.manifest.attach,
                "exec": list(runtime.manifest.exec),
                "restart": runtime.manifest.restart.value,
                "desired": runtime.desired.value,
                "state": runtime.state.value,
                "pid": self._pid(runtime),
                "restart_count": runtime.restart_count,
                "failure_streak": runtime.failure_streak,
                "last_exit": runtime.last_exit,
                "last_error": runtime.last_error,
            }
            if runtime.backoff_until is not None:
                entry["backoff_in_s"] = max(0.0, runtime.backoff_until - monotonic())
            units.append(entry)
        root: dict[str, object] = {
            "pid": os.getpid(),
            "uptime_s": monotonic() - self._started_at if self._started_at is not None else 0.0,
            "unit_count": len(self._units),
            "running": self._running,
        }
        return {"root": root, "units": units, "restarts_total": restarts_total}

    async def dispatch(self, request: RequestPayload) -> ResponsePayload:
        """Serve one validated K1 request; business errors become error codes."""
        verb = Verb(request["verb"])
        name = request.get("name")
        try:
            if verb is Verb.STATUS:
                return ok_response(await self.status())
            if verb is Verb.UPGRADE:
                return error_response(
                    ErrorCode.NOT_IMPLEMENTED,
                    "upgrade is a stub in this slice; the exec-replacement protocol "
                    "lands with a later change",
                )
            if name is None:
                # parse_request already rejects this shape; kept as a guard so a
                # future verb cannot fall through to a confusing failure.
                return error_response(
                    ErrorCode.INVALID_REQUEST, f"verb {verb.value!r} requires a name"
                )
            if verb is Verb.UP:
                return ok_response(await self.up(name))
            if verb is Verb.DOWN:
                return ok_response(await self.down(name))
            if verb is Verb.RESTART:
                return ok_response(await self.restart(name))
        except UnknownUnitError as exc:
            return error_response(ErrorCode.UNKNOWN_UNIT, str(exc))
        raise AssertionError(f"unhandled verb {verb}")

    # ── internals ────────────────────────────────────────────────────────────

    async def _start_unit(self, runtime: _UnitRuntime) -> None:
        """Spawn a stopped unit; a spawn failure is recorded, never raised."""
        if self._is_active(runtime):
            return
        try:
            await self._spawn(runtime)
        except OSError as exc:
            runtime.state = UnitState.STOPPED
            runtime.last_error = f"spawn failed: {exc}"
            _log.error("unit %s failed to start: %s", runtime.manifest.id, exc)
            self._maybe_schedule_restart(runtime)

    async def _spawn(self, runtime: _UnitRuntime) -> None:
        """Fork+exec one fresh generation of `runtime`.

        The caller holds the mutation lock. Units are spawned as plain
        children — no new session, no detaching, `close_fds=True` so the
        instance lock cannot leak into the tree.
        """
        manifest = runtime.manifest
        log_path = self._log_dir / f"{manifest.id}.log"
        log_fd = os.open(log_path, os.O_CREAT | os.O_WRONLY | os.O_APPEND, 0o644)
        try:
            proc = await asyncio.create_subprocess_exec(
                *manifest.exec,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=log_fd,
                stderr=asyncio.subprocess.STDOUT,
                close_fds=True,
            )
        finally:
            os.close(log_fd)
        generation = _Generation(proc=proc, started_at=monotonic())
        runtime.generation = generation
        runtime.state = UnitState.RUNNING
        runtime.last_error = None
        runtime.backoff_until = None
        runtime.watch_task = asyncio.create_task(self._watch(runtime, generation))
        _log.info(
            "unit %s started (pid %s): %s",
            manifest.id,
            proc.pid,
            " ".join(manifest.exec),
        )

    async def _watch(self, runtime: _UnitRuntime, generation: _Generation) -> None:
        """Reap one generation, then apply the restart policy.

        Everything after `await proc.wait()` is synchronous — `exited` must be
        the final action so waiters never observe a half-processed exit.
        """
        returncode = await generation.proc.wait()
        if runtime.generation is generation:
            runtime.generation = None
            runtime.last_exit = _describe_exit(returncode)
            runtime.backoff_until = None
            if runtime.desired is DesiredState.STOPPED or not self._running:
                runtime.state = UnitState.STOPPED
            else:
                lifetime = monotonic() - generation.started_at
                if lifetime >= self._config.stable_after_s:
                    runtime.failure_streak = 0
                if self._should_restart(runtime.manifest, returncode):
                    _log.warning(
                        "unit %s exited (%s); restarting per policy %s",
                        runtime.manifest.id,
                        _describe_exit(returncode),
                        runtime.manifest.restart.value,
                    )
                    self._schedule_restart(runtime)
                else:
                    runtime.state = UnitState.STOPPED
                    _log.info(
                        "unit %s exited (%s); policy %s holds it stopped",
                        runtime.manifest.id,
                        _describe_exit(returncode),
                        runtime.manifest.restart.value,
                    )
        generation.exited.set()

    def _should_restart(self, manifest: UnitManifest, returncode: int) -> bool:
        """Whether an exited generation should be brought back."""
        if manifest.restart is RestartPolicy.ALWAYS:
            return True
        if manifest.restart is RestartPolicy.ON_FAILURE:
            return returncode != 0
        return False

    def _maybe_schedule_restart(self, runtime: _UnitRuntime) -> None:
        """Arm a backoff retry after a failed spawn, when policy allows one."""
        if runtime.desired is not DesiredState.RUNNING or not self._running:
            return
        if runtime.manifest.restart is RestartPolicy.NEVER:
            return
        self._schedule_restart(runtime)

    def _schedule_restart(self, runtime: _UnitRuntime) -> None:
        """Schedule the next start attempt with exponential backoff."""
        existing = runtime.restart_task
        if existing is not None and not existing.done():
            existing.cancel()
        delay = min(
            self._config.backoff_base_s * (2**runtime.failure_streak),
            self._config.backoff_max_s,
        )
        runtime.failure_streak += 1
        runtime.state = UnitState.BACKOFF
        runtime.backoff_until = monotonic() + delay
        _log.info(
            "unit %s restarting in %.2fs (attempt %d)",
            runtime.manifest.id,
            delay,
            runtime.failure_streak,
        )
        runtime.restart_task = asyncio.create_task(self._restart_after(runtime, delay))

    async def _restart_after(self, runtime: _UnitRuntime, delay: float) -> None:
        """The backoff timer: spawn when it elapses, or retire quietly."""
        await asyncio.sleep(delay)
        async with self._lock:
            if not self._running or runtime.desired is not DesiredState.RUNNING:
                return
            if runtime.generation is not None:
                return
            try:
                await self._spawn(runtime)
            except OSError as exc:
                runtime.last_error = f"spawn failed: {exc}"
                runtime.state = UnitState.STOPPED
                _log.error("unit %s restart attempt failed: %s", runtime.manifest.id, exc)
                self._maybe_schedule_restart(runtime)
                return
            runtime.restart_count += 1

    async def _stop_unit(self, runtime: _UnitRuntime) -> None:
        """Terminate a unit's current generation (TERM, then KILL on timeout)."""
        await self._cancel_task(runtime.restart_task)
        runtime.restart_task = None
        generation = runtime.generation
        if generation is None:
            runtime.state = UnitState.STOPPED
            return
        await self._stop_generation(runtime, generation)

    async def _stop_generation(self, runtime: _UnitRuntime, generation: _Generation) -> None:
        """Ask one generation to stop; force the kill after the grace period."""
        proc = generation.proc
        if proc.returncode is None:
            proc.terminate()
        try:
            await asyncio.wait_for(generation.exited.wait(), self._config.stop_timeout_s)
        except TimeoutError:
            _log.warning(
                "unit %s did not stop within %.1fs; killing pid %s",
                runtime.manifest.id,
                self._config.stop_timeout_s,
                proc.pid,
            )
            proc.kill()
            await generation.exited.wait()

    @staticmethod
    async def _cancel_task(task: asyncio.Task[None] | None) -> None:
        """Cancel and await a task if it is still pending (safe on None/done)."""
        if task is None:
            return
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task

    @staticmethod
    def _is_active(runtime: _UnitRuntime) -> bool:
        """True when the unit has a live generation process."""
        generation = runtime.generation
        return generation is not None and generation.proc.returncode is None

    @staticmethod
    def _pid(runtime: _UnitRuntime) -> int | None:
        """The live generation's pid, or None when nothing is running."""
        generation = runtime.generation
        if generation is None or generation.proc.returncode is not None:
            return None
        return generation.proc.pid

    @staticmethod
    def _unit_result(runtime: _UnitRuntime, action: str) -> dict[str, object]:
        """One per-unit entry for a control-verb response."""
        result: dict[str, object] = {
            "id": runtime.manifest.id,
            "action": action,
            "state": runtime.state.value,
            "pid": Supervisor._pid(runtime),
        }
        if runtime.last_error is not None:
            result["error"] = runtime.last_error
        return result


def _describe_exit(returncode: int) -> str:
    """Human-readable exit of a child process (signal deaths are negative)."""
    if returncode < 0:
        return f"signal {-returncode}"
    return f"exit {returncode}"
