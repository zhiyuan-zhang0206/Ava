"""One owner for application service births and native custody.

Children stay in the permission ancestry. Stop captures their native births
before signalling, preserves uncertain custody, and never escalates implicitly.
The health monitor is the sole retry scheduler; an unexplained leader exit
requires reconciliation rather than permission to launch a duplicate.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from time import monotonic
from typing import Protocol

import psutil

from services.ava_root.custody import ServiceCustody, require_clear
from services.ava_root.manifest import (
    DesiredState,
    RestartPolicy,
    UnitManifest,
    UnitRegistry,
    UnknownUnitError,
)
from services.ava_root.windows.process import ApplicationProcess
from shared.env_registry import (
    MANIFEST_CERTIFICATION_FINALIZER_ENV,
    MANIFEST_CERTIFICATION_SECRET_ENV,
    manifest_certification_secret_env,
)
from shared.exec_process_domain import process_group_closed, process_group_pids
from shared.native_process.ownership import OwnedProcess, capture_tree, retain_processes
from shared.process_env import inherited_process_env
from shared.root_control.ipc import (
    ErrorCode,
    RequestPayload,
    ResponsePayload,
    Verb,
    error_response,
    ok_response,
)
from shared.runtime_interpreter import LoadedRuntimeIdentity

_log = logging.getLogger(__name__)

_MANIFEST_FINALIZER_UNIT = "agent-host"


def _unit_env(unit_id: str) -> dict[str, str]:
    """Return one root child env with the proof limited to the finalizer."""
    env = inherited_process_env()
    if unit_id == _MANIFEST_FINALIZER_UNIT:
        env.update(manifest_certification_secret_env())
    else:
        env.pop(MANIFEST_CERTIFICATION_SECRET_ENV, None)
        env.pop(MANIFEST_CERTIFICATION_FINALIZER_ENV, None)
    return env


@dataclass(frozen=True, slots=True)
class SupervisorConfig:
    """Timing policy for the supervise loop (test-tunable)."""

    stop_timeout_s: float = 10.0
    """Grace period; only explicit force permits a later kill."""


class UnitState(StrEnum):
    """What a unit's process is doing right now."""

    RUNNING = "running"
    STOPPED = "stopped"


@dataclass(slots=True)
class _Generation:
    """One process instance of a unit.

    `exited` is set as the *last* action of the wait task, so any coroutine
    that observes the event also observes the exit fully processed.
    """

    proc: asyncio.subprocess.Process | ApplicationProcess
    started_at: float
    identity: OwnedProcess | None = None
    custody: ServiceCustody | None = None
    tracked: set[OwnedProcess] = field(default_factory=set[OwnedProcess])
    closing: bool = False
    exited: asyncio.Event = field(default_factory=asyncio.Event)


@dataclass(slots=True)
class _UnitRuntime:
    """Mutable per-unit state owned by the supervisor."""

    manifest: UnitManifest
    desired: DesiredState = DesiredState.STOPPED
    state: UnitState = UnitState.STOPPED
    generation: _Generation | None = None
    restart_count: int = 0
    last_exit: str | None = None
    last_error: str | None = None
    watch_task: asyncio.Task[None] | None = None


class HealthSource(Protocol):
    """The health runner slice `status()` embeds (W1.2a's `HealthMonitor`)."""

    def health_snapshot(self) -> dict[str, object]:
        """A plain-dict health view, keyed by unit id."""
        ...


class MetricsSource(Protocol):
    """The self-check slice `status()` embeds (W1.2b's `TreeSelfCheck`)."""

    def metrics_snapshot(self) -> dict[str, object]:
        """The B7 metrics block (chain gauges + injectable slots)."""
        ...


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
        run_dir: Path,
        config: SupervisorConfig | None = None,
    ) -> None:
        self._registry = registry
        self._run_dir = run_dir
        self._log_dir = run_dir / "logs"
        self._config = config if config is not None else SupervisorConfig()
        self._units: dict[str, _UnitRuntime] = {
            manifest.id: _UnitRuntime(manifest=manifest) for manifest in registry.units
        }
        self._lock = asyncio.Lock()
        self._started_at: float | None = None
        self._running = False
        self._health: HealthSource | None = None
        self._metrics: MetricsSource | None = None
        self._runtime_identity: LoadedRuntimeIdentity | None = None
        self._home: Path | None = None

    # ── lifecycle ────────────────────────────────────────────────────────────

    async def start(self) -> None:
        """Start one clean root generation; unfinished custody requires recovery."""
        require_clear(self._run_dir)
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
            for task in (runtime.watch_task,)
            if task is not None
        ]
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        for runtime in self._units.values():
            runtime.watch_task = None

    # ── control verbs ────────────────────────────────────────────────────────

    async def up(self, unit_id: str) -> dict[str, object]:
        """Ensure a unit and its subtree are running; idempotent per unit."""
        async with self._lock:
            return await self._up_locked(unit_id)

    async def _up_locked(self, unit_id: str) -> dict[str, object]:
        results: list[dict[str, object]] = []
        for member in self._registry.subtree(unit_id):
            runtime = self._units[member]
            runtime.desired = DesiredState.RUNNING
            if self._is_active(runtime):
                action = "already-running"
            else:
                await self._start_unit(runtime)
                action = "started" if self._is_active(runtime) else "failed"
            results.append(self._unit_result(runtime, action))
        return {"verb": Verb.UP.value, "units": results}

    async def down(self, unit_id: str, *, force: bool = False) -> dict[str, object]:
        """Stop a unit and its subtree, children before parents."""
        async with self._lock:
            return await self._down_locked(unit_id, force=force)

    async def _down_locked(self, unit_id: str, *, force: bool = False) -> dict[str, object]:
        results: list[dict[str, object]] = []
        for member in self._registry.stop_order(unit_id):
            runtime = self._units[member]
            runtime.desired = DesiredState.STOPPED
            was_active = self._is_active(runtime)
            await self._stop_unit(runtime, force=force)
            action = "stopped" if was_active else "already-stopped"
            results.append(self._unit_result(runtime, action))
        return {"verb": Verb.DOWN.value, "units": results}

    async def restart(self, unit_id: str) -> dict[str, object]:
        """Stop then replace a subtree under one mutation lock.

        Planned downtime avoids overlapping writers. Spawn acceptance is not
        readiness; callers must observe the new generation.
        """
        async with self._lock:
            await self._down_locked(unit_id)
            result = await self._up_locked(unit_id)
            for member in self._registry.subtree(unit_id):
                self._units[member].restart_count += 1
            return result | {"verb": Verb.RESTART.value}

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
                "manifest_digest": runtime.manifest.digest(),
                "restart": runtime.manifest.restart.value,
                "desired": runtime.desired.value,
                "state": runtime.state.value,
                "pid": self._pid(runtime),
                "create_time": runtime.generation.identity.birth
                if runtime.generation and runtime.generation.identity
                else None,
                "starttime": runtime.generation.identity.starttime
                if runtime.generation and runtime.generation.identity
                else None,
                "restart_count": runtime.restart_count,
                "last_exit": runtime.last_exit,
                "last_error": runtime.last_error,
            }
            units.append(entry)
        own_identity = OwnedProcess.capture(psutil.Process())
        root: dict[str, object] = {
            "pid": own_identity.pid,
            "create_time": own_identity.birth,
            "starttime": own_identity.starttime,
            "uptime_s": monotonic() - self._started_at if self._started_at is not None else 0.0,
            "unit_count": len(self._units),
            "launch_digest": self._registry.launch_digest,
            "running": self._running,
            "home": str(self._home) if self._home is not None else None,
            "runtime": self._runtime_identity.model_dump(mode="json")
            if self._runtime_identity
            else None,
        }
        snapshot: dict[str, object] = {
            "root": root,
            "units": units,
            "restarts_total": restarts_total,
        }
        if self._health is not None:
            snapshot["health"] = self._health.health_snapshot()
        if self._metrics is not None:
            snapshot["metrics"] = self._metrics.metrics_snapshot()
        return snapshot

    def bind_runtime(self, identity: LoadedRuntimeIdentity, *, home: Path) -> None:
        """Deployment wiring binds loaded code once, before any service birth."""
        if self._running or self._runtime_identity is not None:
            raise RuntimeError("root runtime identity is already bound or running")
        if not home.is_absolute() or home.resolve(strict=True) != home:
            raise RuntimeError("root runtime home is not canonical")
        self._home = home
        self._runtime_identity = identity

    def attach_health(self, source: HealthSource) -> None:
        """Embed the health runner's snapshot in `status()`; absent until wired."""
        self._health = source

    def attach_metrics(self, source: MetricsSource) -> None:
        """Embed the self-check's metrics in `status()`; absent until wired."""
        self._metrics = source

    def tree_view(self) -> dict[str, object]:
        """Raw per-unit facts for the self-check: `{"root_pid", "units": [...]}`.

        Unlike `status()`, the pid is the generation's *recorded* pid even after
        its process exited (waiting for the watch task) — the self-check
        verifies the tree's claims against the OS, so the claim must stay
        visible; `status()` masks a dead generation's pid to None.
        """
        units: list[dict[str, object]] = [
            {
                "id": runtime.manifest.id,
                "state": runtime.state.value,
                "pid": None if runtime.generation is None else runtime.generation.proc.pid,
            }
            for runtime in self._units.values()
        ]
        return {"root_pid": os.getpid(), "units": units}

    def health_generation(self, unit_id: str) -> tuple[OwnedProcess, float] | None:
        """The retained native generation and its monotonic launch time."""
        runtime = self._units.get(unit_id)
        if runtime is None:
            raise UnknownUnitError(f"unknown unit {unit_id!r}")
        generation = runtime.generation
        if generation is None or generation.identity is None:
            return None
        return generation.identity, generation.started_at

    def revival_deferral(self, unit_id: str) -> str | None:
        """Preserve explicit stop, retained custody, and never-restart policy."""
        runtime = self._units.get(unit_id)
        if runtime is None:
            raise UnknownUnitError(f"unknown unit {unit_id!r}")
        if runtime.desired is not DesiredState.RUNNING:
            return "held down"
        if runtime.generation is not None and not self._is_active(runtime):
            return "native custody requires reconciliation"
        if runtime.manifest.restart is RestartPolicy.NEVER:
            return "policy never"
        return None

    async def dispatch(self, request: RequestPayload) -> ResponsePayload:
        """Serve one validated K1 request; business errors become error codes."""
        verb = Verb(request["verb"])
        name = request.get("name")
        try:
            if verb is Verb.STATUS:
                return ok_response(await self.status())
            if name is None:
                # parse_request already rejects this shape; kept as a guard so a
                # future verb cannot fall through to a confusing failure.
                return error_response(
                    ErrorCode.INVALID_REQUEST, f"verb {verb.value!r} requires a name"
                )
            if verb is Verb.UP:
                return ok_response(await self.up(name))
            if verb in {Verb.DOWN, Verb.FORCE_DOWN}:
                return ok_response(await self.down(name, force=verb is Verb.FORCE_DOWN))
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

    async def _spawn(self, runtime: _UnitRuntime) -> None:
        """Fork+exec one fresh generation of `runtime`.

        The caller holds the mutation lock. Units are spawned as plain
        children, each leading its own process group (setpgid, never a new
        session, so the permission ancestry and session are root's), with
        `close_fds=True` so the instance lock cannot leak into the tree. The
        group is the unit's stop scope: see `_stop_posix_generation`.
        """
        manifest = runtime.manifest
        for item in manifest.inputs:
            item.require_unchanged()
        # One directory per unit (G5): the unit owns its log space, so naming /
        # rotation policy can land inside it without another layout change.
        log_path = self._log_dir / manifest.id / "output.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_fd = os.open(log_path, os.O_CREAT | os.O_WRONLY | os.O_APPEND, 0o644)
        try:
            custody = ServiceCustody(self._run_dir, manifest.id)
            env = _unit_env(manifest.id) | dict(manifest.env)
            if os.name == "nt":
                from services.ava_root.windows.process import spawn

                proc = spawn(list(manifest.exec), env, log_fd)
            else:
                proc = await asyncio.create_subprocess_exec(
                    *manifest.exec,
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=log_fd,
                    stderr=asyncio.subprocess.STDOUT,
                    env=env,
                    close_fds=True,
                    process_group=0,
                )
        finally:
            os.close(log_fd)
        try:
            identity = OwnedProcess.capture(psutil.Process(proc.pid))
        except psutil.NoSuchProcess:
            identity = None
        generation = _Generation(
            proc=proc,
            started_at=monotonic(),
            identity=identity,
            custody=custody,
            tracked={identity} if identity else set(),
        )
        if identity is not None:
            custody.retain(generation.tracked)
        runtime.generation = generation
        runtime.state = UnitState.RUNNING
        runtime.last_error = None
        runtime.watch_task = asyncio.create_task(self._watch(runtime, generation))
        _log.info(
            "unit %s started (pid %s): %s",
            manifest.id,
            proc.pid,
            " ".join(manifest.exec),
        )

    async def _watch(self, runtime: _UnitRuntime, generation: _Generation) -> None:
        """Reap one generation, retaining unexpected native custody.

        Everything after `await proc.wait()` is synchronous — `exited` must be
        the final action so waiters never observe a half-processed exit.
        """
        returncode = await generation.proc.wait()
        if runtime.generation is generation:
            runtime.last_exit = _describe_exit(returncode)
            runtime.state = UnitState.STOPPED
            if not generation.closing:
                runtime.last_error = "unexpected exit; native custody requires reconciliation"
        # A dead leader cannot prove that its descendants are gone. Only the
        # stop owner releases custody after observing its captured scope close.
        generation.exited.set()

    async def _stop_unit(self, runtime: _UnitRuntime, *, force: bool = False) -> None:
        """Stop one owned generation; force escalation must be explicit."""
        generation = runtime.generation
        if generation is None:
            runtime.state = UnitState.STOPPED
            return
        await self._stop_generation(runtime, generation, force=force)

    async def _stop_generation(
        self, runtime: _UnitRuntime, generation: _Generation, *, force: bool = False
    ) -> None:
        """Stop captured births; unknown scope keeps custody and refuses success."""
        if isinstance(generation.proc, ApplicationProcess):
            await self._stop_job_generation(runtime, generation, generation.proc, force=force)
            return
        identity, custody = self._capture_posix_stop(runtime, generation)
        await self._stop_posix_generation(runtime, generation, identity, custody, force=force)

    async def _stop_job_generation(
        self,
        runtime: _UnitRuntime,
        generation: _Generation,
        process: ApplicationProcess,
        *,
        force: bool,
    ) -> None:
        custody = generation.custody
        if custody is None:
            raise RuntimeError("application Job has no durable custody")
        generation.closing = True
        await process.close(custody, timeout=self._config.stop_timeout_s, force=force)
        await generation.exited.wait()
        custody.clear()
        process.job.close()
        runtime.generation = None
        runtime.state = UnitState.STOPPED

    @staticmethod
    def _capture_posix_stop(
        runtime: _UnitRuntime,
        generation: _Generation,
    ) -> tuple[OwnedProcess, ServiceCustody]:
        identity, custody = generation.identity, generation.custody
        if identity is None or custody is None:
            raise RuntimeError(f"unit {runtime.manifest.id} has unacknowledged native birth")
        if not generation.closing and not identity.live():
            raise RuntimeError(
                f"unit {runtime.manifest.id} exited before scope capture; custody retained"
            )
        retain_processes(generation.tracked, capture_tree(identity))
        custody.retain(generation.tracked)
        generation.closing = True
        return identity, custody

    async def _stop_posix_generation(
        self,
        runtime: _UnitRuntime,
        generation: _Generation,
        identity: OwnedProcess,
        custody: ServiceCustody,
        *,
        force: bool,
    ) -> None:
        """Bounded TERM, then certified closure of the unit's process group.

        The leader leads the unit's group; its group number stays reserved
        while any member exists. After the leader is reaped, only the kernel
        reporting that group empty certifies the stop: a child forked while
        the leader handled TERM is still a member, so it is captured, signalled
        like any tracked descendant and must exit too. Only explicit force
        escalates, and only to those captured members. A member that calls
        setsid() leaves the group by construction and is not covered.
        """
        self._signal_owned(identity, force=False)
        deadline = monotonic() + self._config.stop_timeout_s
        while True:
            living = {item for item in generation.tracked if item.live()}
            if not living:
                await generation.exited.wait()
                if process_group_closed(identity.pid):
                    custody.clear()
                    runtime.generation = None
                    runtime.state = UnitState.STOPPED
                    return
                living = _capture_group(generation.tracked, identity.pid)
            for item in living:
                retain_processes(generation.tracked, capture_tree(item))
            custody.retain(generation.tracked)
            expired = monotonic() >= deadline
            if expired and not force:
                raise _ownership_retained(runtime.manifest.id, living, identity.pid)
            if not identity.live() or expired:
                self._signal_all(living, force=expired and force)
            if expired:
                force = False
                deadline = monotonic() + self._config.stop_timeout_s
            await asyncio.sleep(0.05)

    @staticmethod
    def _signal_owned(identity: OwnedProcess, *, force: bool) -> None:
        identity.send_signal(signal.SIGKILL if force else signal.SIGTERM)

    @classmethod
    def _signal_all(cls, living: set[OwnedProcess], *, force: bool) -> None:
        for item in living:
            cls._signal_owned(item, force=force)

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


def _ownership_retained(unit_id: str, living: set[OwnedProcess], pgid: int) -> RuntimeError:
    survivors = sorted(item.pid for item in living) or process_group_pids(pgid)
    return RuntimeError(f"unit {unit_id} did not stop; ownership retained (pids {survivors})")


def _capture_group(tracked: set[OwnedProcess], pgid: int) -> set[OwnedProcess]:
    """Retain the named members of an occupied unit group; return the live ones."""
    members: set[OwnedProcess] = set()
    for pid in process_group_pids(pgid):
        with contextlib.suppress(psutil.NoSuchProcess):
            members.add(OwnedProcess.capture(psutil.Process(pid)))
    retain_processes(tracked, members)
    return {item for item in members if item.live()}


def _describe_exit(returncode: int) -> str:
    """Human-readable exit of a child process (signal deaths are negative)."""
    if returncode < 0:
        return f"signal {-returncode}"
    return f"exit {returncode}"
