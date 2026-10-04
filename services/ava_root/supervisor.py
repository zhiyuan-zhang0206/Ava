"""One owner for application service births and native custody.

Children stay in the permission ancestry. Stop captures their native births
before signalling, preserves uncertain custody, and never escalates implicitly.
The health monitor is the sole retry scheduler; an unexplained leader exit
requires reconciliation rather than permission to launch a duplicate.
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from time import monotonic, time
from typing import Protocol

import psutil

from base.deploy.release.runtime_interpreter import LoadedRuntimeIdentity
from base.native_process.child_env import inherited_process_env
from base.native_process.ownership import OwnedProcess
from base.native_process.root_control.ipc import (
    ErrorCode,
    RequestPayload,
    ResponsePayload,
    Verb,
    error_response,
    ok_response,
)
from services.ava_root import intent_store
from services.ava_root.custody import require_clear
from services.ava_root.failure_state import UnitFailureFacts
from services.ava_root.group_scope import group_closed, record_survivors
from services.ava_root.intent_store import (
    IntentRecord,
    IntentSource,
    RestartFailure,
    RestartStage,
    UnitIntent,
    merge_record_for_boot,
)
from services.ava_root.manifest import (
    RestartPolicy,
    UnitRegistry,
    UnitState,
    UnknownUnitError,
)
from services.ava_root.reconciling import ReconcilingMixin
from services.ava_root.stopping import StoppingMixin, SupervisorConfig
from services.ava_root.unit_records import _Generation, _UnitRuntime

_log = logging.getLogger(__name__)


class HealthSource(Protocol):
    """The health runner slice `status()` embeds (W1.2a's `HealthMonitor`)."""

    def health_snapshot(self) -> dict[str, object]:
        """A plain-dict health view, keyed by unit id."""
        ...


class MetricsSource(Protocol):
    """The self-check slice `status()` embeds (W1.2b's `TreeSelfCheck`)."""

    def metrics_snapshot(self) -> dict[str, object]:
        """The B7 metrics block (the chain gauges)."""
        ...


class Supervisor(StoppingMixin, ReconcilingMixin):
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
        self._reconcile_reports: dict[str, str] = {}
        self._lock = asyncio.Lock()
        self._started_at: float | None = None
        self._running = False
        self._health: HealthSource | None = None
        self._metrics: MetricsSource | None = None
        self._runtime_identity: LoadedRuntimeIdentity | None = None
        self._home: Path | None = None

    # ── lifecycle ────────────────────────────────────────────────────────────

    async def start(self) -> None:
        """Start one clean root generation; unresolved custody reconciles first.

        The custody gate clears every record it can prove gone and refuses only
        on one that keeps an unproven fact, naming its steps and evidence (see
        `custody.require_clear`). Each unit's stored record is merged first
        (conservative, see
        `intent_store.merge_record_for_boot`): a recorded operator stop holds
        its unit down; a stop the root gave itself is superseded; a recorded
        replacement failure is carried until a fresh generation proves it gone.
        """
        require_clear(self._run_dir)
        self._log_dir.mkdir(parents=True, exist_ok=True)
        async with self._lock:
            self._running = True
            self._started_at = monotonic()
            for runtime in self._units.values():
                self._boot_intent(runtime)
            await asyncio.gather(
                *(
                    self._start_unit(runtime)
                    for runtime in self._units.values()
                    if runtime.intent is UnitIntent.RUNNING
                )
            )

    async def shutdown(self) -> None:
        """Stop the whole tree (children before parents) and drain the tasks.

        A refused unit keeps its generation and custody, and the units after it
        are still stopped; the refusals are then raised together, before any
        watch is cancelled, so each refused unit's reap is still observed.
        """
        refusals: list[Exception] = []
        async with self._lock:
            self._running = False
            for unit_id in self._registry.all_stop_order():
                runtime = self._units[unit_id]
                self._set_intent(runtime, UnitIntent.STOPPED, IntentSource.SELF)
                try:
                    await self._stop_unit(runtime)
                except Exception as exc:
                    refusals.append(exc)
        if refusals:
            raise ExceptionGroup(
                f"root shutdown retained custody of {len(refusals)} unit(s)", refusals
            )
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
            self._set_intent(runtime, UnitIntent.RUNNING, IntentSource.OPERATOR)
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
            self._set_intent(runtime, UnitIntent.STOPPED, IntentSource.OPERATOR)
            was_active = self._is_active(runtime)
            await self._stop_unit(runtime, force=force)
            action = "stopped" if was_active else "already-stopped"
            results.append(self._unit_result(runtime, action))
        return {"verb": Verb.DOWN.value, "units": results}

    async def restart(self, unit_id: str) -> dict[str, object]:
        """Stop then replace a subtree under one mutation lock.

        A restart means "keep it running, replace it": it never turns a failed
        self-rescue into a stop, and it never rewrites an explicit stop into a
        run unless this verb is that operator word. Each member ends either with
        a fresh active generation or with an explicit `restart_failed` state
        (intent stays running) that the health monitor counts and retries under
        its backoff. A member whose stop was refused keeps its old generation
        while the rest of the subtree is still processed; refusals are raised
        together afterwards. Spawn acceptance is not readiness; callers must
        observe the new generation.
        """
        async with self._lock:
            members = self._registry.subtree(unit_id)
            for member in members:
                runtime = self._units[member]
                if runtime.intent is not UnitIntent.RUNNING:
                    self._set_intent(runtime, UnitIntent.RUNNING, IntentSource.OPERATOR)
            refusals: list[Exception] = []
            refused: set[str] = set()
            for member in self._registry.stop_order(unit_id):
                runtime = self._units[member]
                try:
                    await self._stop_unit(runtime)
                except Exception as exc:
                    refused.add(member)
                    refusals.append(exc)
                    self._record_restart_failure(runtime, RestartStage.DOWN, str(exc))
            results: list[dict[str, object]] = []
            for member in members:
                runtime = self._units[member]
                if member in refused:
                    results.append(self._unit_result(runtime, "refused"))
                    continue
                try:
                    await self._start_unit(runtime)
                except Exception as exc:
                    runtime.last_error = f"restart failed: {exc}"
                    self._record_restart_failure(runtime, RestartStage.UP, str(exc))
                    results.append(self._unit_result(runtime, "failed"))
                    continue
                if self._is_active(runtime):
                    runtime.restart_count += 1
                    results.append(self._unit_result(runtime, "restarted"))
                else:
                    detail = runtime.last_error or "replacement did not start"
                    self._record_restart_failure(runtime, RestartStage.UP, detail)
                    results.append(self._unit_result(runtime, "failed"))
            if len(refusals) == 1:
                raise refusals[0]
            if refusals:
                raise ExceptionGroup(
                    f"root restart refused to stop {len(refusals)} unit(s)", refusals
                )
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
                "manifest_digest": runtime.manifest.digest(),
                "restart": runtime.manifest.restart.value,
                "intent": runtime.intent.value,
                "intent_source": runtime.intent_source.value,
                "restart_failed": None
                if runtime.restart_failed is None
                else {
                    "stage": runtime.restart_failed.stage.value,
                    "since": runtime.restart_failed.since,
                    "detail": runtime.restart_failed.detail,
                },
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
        """Preserve explicit stop, retained custody, and never-restart policy.

        Classification reads the unit's intent — the policy fact — never a
        mechanical transition residue: only an explicit stop is the expected,
        silent state. A recorded replacement failure is deliberately not a
        deferral: suppressing the action here would recreate exactly the
        silence class the record exists to surface (task #4872) — its retry
        cadence belongs to the health monitor's backoff.
        """
        runtime = self._units.get(unit_id)
        if runtime is None:
            raise UnknownUnitError(f"unknown unit {unit_id!r}")
        if runtime.intent is not UnitIntent.RUNNING:
            return "held down"
        if runtime.generation is not None and not self._is_active(runtime):
            return "native custody requires reconciliation"
        if runtime.manifest.restart is RestartPolicy.NEVER:
            return "policy never"
        return None

    def unit_failure_facts(self, unit_id: str) -> UnitFailureFacts:
        """The failure-state facts of one unit: intent, recorded failure, custody.

        `custody_held` reads `revival_deferral`'s reconciliation clause — a
        retained generation that is not active holds revival until reconciled.
        """
        runtime = self._units.get(unit_id)
        if runtime is None:
            raise UnknownUnitError(f"unknown unit {unit_id!r}")
        return UnitFailureFacts(
            intent_running=runtime.intent is UnitIntent.RUNNING,
            restart_failed=runtime.restart_failed,
            custody_held=runtime.generation is not None and not self._is_active(runtime),
        )

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

    def _set_intent(self, runtime: _UnitRuntime, intent: UnitIntent, source: IntentSource) -> None:
        """Record a unit's policy fact and persist it for the next root."""
        runtime.intent = intent
        runtime.intent_source = source
        self._write_record(runtime)

    def _write_record(self, runtime: _UnitRuntime) -> None:
        intent_store.write(
            self._run_dir,
            runtime.manifest.id,
            IntentRecord(runtime.intent, runtime.intent_source, runtime.restart_failed),
        )

    def _boot_intent(self, runtime: _UnitRuntime) -> None:
        """Merge this unit's stored record into a fresh root generation."""
        record = intent_store.read(self._run_dir, runtime.manifest.id)
        merged = merge_record_for_boot(record)
        if merged.note is not None:
            _log.info("unit %s: %s", runtime.manifest.id, merged.note)
        runtime.intent = merged.intent
        runtime.intent_source = merged.source
        runtime.restart_failed = merged.restart_failed
        if record is None or (
            record.intent,
            record.source,
            record.restart_failed,
        ) != (merged.intent, merged.source, merged.restart_failed):
            self._write_record(runtime)

    def _record_restart_failure(
        self, runtime: _UnitRuntime, stage: RestartStage, detail: str
    ) -> None:
        """Record one member's failed replacement half; store first, then signal."""
        previous = runtime.restart_failed
        since = previous.since if previous is not None else time()
        failure = RestartFailure(stage=stage, since=since, detail=detail)
        runtime.restart_failed = failure
        self._write_record(runtime)
        if previous is None or previous.stage is not stage:
            self._emit_restart_failed(runtime, failure)

    def _clear_restart_failure(self, runtime: _UnitRuntime) -> None:
        """Clear a recorded failure once a fresh generation proved it gone."""
        previous = runtime.restart_failed
        if previous is None:
            return
        runtime.restart_failed = None
        self._write_record(runtime)
        self._emit_restart_cleared(runtime, previous)

    @staticmethod
    def _emit_restart_failed(runtime: _UnitRuntime, failure: RestartFailure) -> None:
        try:
            from base.log import logger

            logger.warning(
                "unit {unit} replacement failed at its {stage} half — explicit failure state "
                "recorded; intent stays running and the health monitor retries under its backoff",
                event="root_restart_failed",
                unit=runtime.manifest.id,
                stage=failure.stage.value,
                detail=failure.detail,
            )
        except Exception:
            # Fan-out may lag or fail; the record is already stored, so never
            # let a signal failure corrupt the transition that produced it.
            _log.exception("unit %s: restart-failure event fan-out failed", runtime.manifest.id)

    @staticmethod
    def _emit_restart_cleared(runtime: _UnitRuntime, failure: RestartFailure) -> None:
        try:
            from base.log import logger

            logger.info(
                "unit {unit} replacement succeeded — failure state cleared after "
                "{failed_for_s:.0f}s",
                event="root_restart_cleared",
                unit=runtime.manifest.id,
                failed_for_s=max(0.0, time() - failure.since),
            )
        except Exception:
            _log.exception("unit %s: restart-cleared event fan-out failed", runtime.manifest.id)

    async def _start_unit(self, runtime: _UnitRuntime) -> None:
        """Spawn a stopped unit; a spawn failure is recorded, never raised.

        A fresh active generation is the only proof that a recorded replacement
        failure is gone, so success clears it on every activation path.
        """
        if self._is_active(runtime):
            return
        try:
            await self._spawn(runtime)
        except OSError as exc:
            runtime.state = UnitState.STOPPED
            runtime.last_error = f"spawn failed: {exc}"
            _log.error("unit %s failed to start: %s", runtime.manifest.id, exc)
            return
        self._clear_restart_failure(runtime)

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
            custody = self._new_custody(manifest.id)
            env = inherited_process_env() | dict(manifest.env)
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
            custody.retain(generation.tracked, proc.pid)
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
        # asyncio's child watcher reaped the leader with waitpid a few loop
        # turns before this resumes, on every POSIX host. A live member keeps the
        # number reserved, so an empty group is the unit's closure and the
        # members listed are its survivors; only a group that empties inside
        # that window, its number then taken by another process, misleads this
        # read. Once the survivors exit, a later group with that number may be a
        # stranger's, so no later stop signals by it.
        pgid = generation.proc.pid
        generation.scope_closed_at_exit = group_closed(pgid)
        if not generation.scope_closed_at_exit:
            record_survivors(runtime.manifest.id, pgid, generation.tracked, generation.custody)
        if runtime.generation is generation:
            runtime.last_exit = _describe_exit(returncode)
            runtime.state = UnitState.STOPPED
            if not generation.closing:
                runtime.last_error = "unexpected exit; native custody requires reconciliation"
        # Only a stop owner releases custody (`_stop_exited_generation` for an
        # unexpected exit); until then it blocks revival and a duplicate root.
        generation.exited.set()

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
