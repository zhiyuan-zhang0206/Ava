"""Root-owned service health, with bounded observation and finite process restarts.

Only observed DOWN results can request replacement through the supervisor.
Unknown identity, inspection errors, timeouts, and foreign listeners are terminal
observations until fresh evidence resolves them. Health is the service retry scheduler.
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
from collections.abc import Callable, Mapping
from concurrent.futures import Future
from contextlib import suppress
from dataclasses import dataclass, replace
from threading import Event, Thread
from typing import Protocol

from base.daemon.health import DaemonProbe
from base.native_process.ownership import OwnedProcess
from services.supervision.ava_root.failure_state import UnitFailureFacts, derive_kind, describe
from services.supervision.ava_root.manifest import UnknownUnitError
from services.supervision.ava_root.probes import Probe, ProbeError, ProbeRegistry

_log = logging.getLogger(__name__)


def _monotonic() -> float:
    """Wall-independent startup clock, replaceable in tests."""
    return time.monotonic()


class RevivalHost(Protocol):
    """The supervisor slice the health runner needs (duck-typed for stubs)."""

    async def restart(self, unit_id: str) -> dict[str, object]:
        """Replace a generation through the supervisor's bounded child lifecycle."""
        ...

    def health_generation(self, unit_id: str) -> tuple[OwnedProcess, float] | None:
        """Retained native birth and monotonic start; never recaptured from a PID."""
        ...

    def revival_deferral(self, unit_id: str) -> str | None:
        """Why a would-be reviver must not act on `unit_id` now, or None."""
        ...

    def unit_failure_facts(self, unit_id: str) -> UnitFailureFacts:
        """The failure-state facts of one unit; unknown units raise UnknownUnitError."""
        ...


HealthGate = Callable[[], tuple[bool, str]]
"""A blocking-policy seam: `(allowed, why)`; absent means always allowed.

Deployment may supply an explicit admission decision. External transition
controllers do not run inside this service observer.
"""


@dataclass(frozen=True, slots=True)
class HealthConfig:
    """Timing policy for the health runner (seconds unless noted)."""

    interval_s: float = 60.0
    """Delay between completed rounds."""

    probe_timeout_s: float = 20.0
    """Maximum wait for one observation; an overdue worker is never duplicated."""

    startup_grace_s: float = 20.0
    """Generic generation startup budget; deployment supplies per-unit readiness tiers."""

    max_restart_attempts: int = 3
    """Actual restarts allowed during one continuous outage, per root process."""

    def __post_init__(self) -> None:
        if not math.isfinite(self.startup_grace_s) or self.startup_grace_s < 0:
            raise ValueError("startup_grace_s must be nonnegative")
        if self.probe_timeout_s <= 0:
            raise ValueError("probe_timeout_s must be positive")
        if self.interval_s <= 0:
            raise ValueError("interval_s must be positive")
        if self.max_restart_attempts < 1:
            raise ValueError("max_restart_attempts must be at least 1")


@dataclass(slots=True)
class UnitHealth:
    """One unit's accumulated health state — the runner's whole memory of it."""

    generation: tuple[OwnedProcess, float] | None = None
    """Retained native generation to which the latest observation belongs."""

    generation_ready: bool = False
    """Whether this generation has produced a fresh ALIVE observation."""

    restart_attempts: int = 0
    """Actual automatic restart calls since ALIVE or an observed operator stop."""

    last_verdict: str | None = None
    """Value of the last probe verdict (`alive` / `down` / `port-taken` / `unavailable`)."""

    last_detail: str = ""
    """Detail string of the last probe verdict."""


# Explicit stop is expected; every other deferral remains visible as a warning.
_HELD_DOWN = "held down"
_NO_REVIVAL_VERB = "no revival verb (not a tree unit)"


class _ProbeObservation:
    """Own one native observation through its real completion or bounded join."""

    def __init__(self, probe: Probe) -> None:
        self.future: Future[DaemonProbe] = Future()
        self._probe = probe
        self._stop = Event()
        self._error: BaseException | None = None
        self._thread = Thread(target=self._run, name="root-probe", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        try:
            if self._stop.is_set():
                self.future.set_result(DaemonProbe.unavailable("observation admission closed"))
            else:
                self.future.set_result(self._sample())
        except BaseException as exc:
            self._error = exc
            _log.error("root observation worker failed", exc_info=exc)
            self.future.set_exception(exc)

    def _sample(self) -> DaemonProbe:
        """Inspection failures remain unknown evidence, never recovery evidence."""
        try:
            result = self._probe()
        except Exception as exc:
            return DaemonProbe.unavailable(f"probe raised {type(exc).__name__}: {exc}")
        if not isinstance(result, DaemonProbe):
            return DaemonProbe.unavailable("probe did not return DaemonProbe")
        return result

    def close(self) -> None:
        self._stop.set()

    def join(self, timeout_s: float) -> bool:
        """Stop admission and observe the real worker; native calls may remain live."""
        self.close()
        self._thread.join(timeout=timeout_s)
        complete = not self._thread.is_alive()
        if self._error is not None:
            raise self._error
        return complete


class ProbeRunner:
    """One bounded synchronous observation, with at most one daemon worker.

    Python cannot cancel a blocked native call. A deadline therefore reports
    UNAVAILABLE and retains the worker until it exits. Later rounds cannot
    replace it; its late result is discarded before a fresh observation starts.
    Daemon threads cannot hold root shutdown hostage.
    """

    def __init__(self) -> None:
        self._observation: _ProbeObservation | None = None
        self._closed = False

    def close(self) -> None:
        """Prevent fresh observations before any worker join begins."""
        self._closed = True
        if self._observation is not None:
            self._observation.close()

    def stop(self, timeout_s: float = 0.2) -> bool:
        """Bounded best effort; a late worker error remains owned on repeated stop."""
        if not math.isfinite(timeout_s) or timeout_s < 0:
            raise ValueError("probe join timeout must be finite and nonnegative")
        self.close()
        return self._observation is None or self._observation.join(timeout_s)

    @staticmethod
    def _retrieve(future: asyncio.Future[DaemonProbe]) -> None:
        # A timed-out/cancelled caller does not await this wrapper again. The
        # native observation retains and raises the same original error at stop.
        if not future.cancelled():
            future.exception()

    async def observe(self, probe: Probe, timeout_s: float) -> DaemonProbe:
        """One verdict; an observation still running at its deadline is UNAVAILABLE."""
        if self._closed:
            raise RuntimeError("probe runner admission closed")
        if self._observation is not None and not self._observation.future.done():
            return DaemonProbe.unavailable("previous observation still running after its deadline")
        if self._observation is not None and not self._observation.join(0):
            return DaemonProbe.unavailable("previous observation worker still completing")
        observation = _ProbeObservation(probe)
        self._observation = observation
        pending = asyncio.wrap_future(observation.future)
        pending.add_done_callback(self._retrieve)
        try:
            result = await asyncio.wait_for(asyncio.shield(pending), timeout_s)
        except TimeoutError:
            return DaemonProbe.unavailable(f"observation exceeded {timeout_s:g}s deadline")
        return result


class HealthMonitor:
    """One probe round over every registered unit, acting per policy.

    Rounds run sequentially — one round is a bounded stream of probes — and each
    unit reports expected probe unavailability as a verdict. Unexpected round
    errors propagate to the root lifecycle. Per-unit state lives in this
    process; a fresh process starts without observation history.
    """

    def __init__(
        self,
        supervisor: RevivalHost,
        registry: ProbeRegistry,
        *,
        config: HealthConfig | None = None,
        tasks: asyncio.TaskGroup | None = None,
        gate: HealthGate | None = None,
        startup_graces: Mapping[str, float] | None = None,
    ) -> None:
        self._supervisor = supervisor
        self._registry = registry
        self._config = config if config is not None else HealthConfig()
        self._gate = gate
        self._startup_graces = dict(startup_graces or {})
        if any(not math.isfinite(value) or value < 0 for value in self._startup_graces.values()):
            raise ValueError("startup grace overrides must be nonnegative")
        self._units: dict[str, UnitHealth] = {}
        self._runners: dict[str, ProbeRunner] = {}
        self._task: asyncio.Task[None] | None = None
        self._tasks = tasks
        self.unfinished_probes: tuple[str, ...] = ()
        self._closed = False

    async def run_round(self) -> None:
        """Probe every unit once, in registration order."""
        if self._closed:
            raise RuntimeError("root health admission closed")
        for unit_id in self._registry.unit_ids():
            await self._check_unit(unit_id)
        self._report_failure_states()

    def _report_failure_states(self) -> None:
        """Emit one `root_unit_failure_state` per unit sitting in a failure state.

        The condition is a state, so it is reported on every round while it
        holds. Units outside the supervisor's tree (the static probe path) carry
        no failure facts and are skipped.
        """
        for unit_id in self._registry.unit_ids():
            try:
                facts = self._supervisor.unit_failure_facts(unit_id)
            except UnknownUnitError:
                continue
            state = self._units.get(unit_id)
            kind = derive_kind(
                facts,
                restart_limit_reached=state is not None
                and state.restart_attempts >= self._config.max_restart_attempts,
            )
            if kind is None:
                continue
            from base.log import logger

            logger.warning(
                "[health] unit {unit}: failure state {kind} ({detail})",
                event="root_unit_failure_state",
                unit=unit_id,
                kind=kind.value,
                detail=describe(facts, kind, state.last_detail if state is not None else ""),
            )

    async def start(self) -> None:
        """Run rounds until `stop()` — sleep first, then one round per interval."""
        if self._task is not None:
            raise RuntimeError("health monitor already started")
        if self._closed:
            raise RuntimeError("root health admission closed")
        if self._tasks is None:
            raise RuntimeError("monitor requires the root participant task group")
        self._task = self._tasks.create_task(self._loop())

    async def stop(self) -> None:
        """Close probe admission and stop all owned work within one join budget."""
        self._closed = True
        for runner in self._runners.values():
            runner.close()
        task = self._task
        self._task = None
        failures: list[BaseException] = []
        if task is not None:
            task.cancel()
            try:
                with suppress(asyncio.CancelledError):
                    await task
            except BaseException as exc:
                failures.append(exc)
        deadline = time.monotonic() + 0.2
        unfinished: list[str] = []
        for unit_id, runner in self._runners.items():
            try:
                if not runner.stop(max(0.0, deadline - time.monotonic())):
                    unfinished.append(unit_id)
            except BaseException as exc:
                failures.append(exc)
        self.unfinished_probes = tuple(unfinished)
        if unfinished:
            _log.warning("root health stopped with unfinished observations: %s", unfinished)
        if failures:
            raise BaseExceptionGroup("root health teardown failed", failures)

    def snapshot(self) -> dict[str, UnitHealth]:
        """A copy of every known unit's health state, keyed by unit id."""
        return {unit_id: replace(state) for unit_id, state in self._units.items()}

    def health_snapshot(self) -> dict[str, object]:
        """A plain-dict view for `Supervisor.status()` (the B7 health surface)."""
        units: dict[str, object] = {}
        for unit_id, state in self._units.items():
            units[unit_id] = {
                "startup_remaining_s": self._startup_remaining(unit_id, state),
                "generation_ready": state.generation_ready,
                "restart_attempts": state.restart_attempts,
                "restart_limit_reached": (
                    state.restart_attempts >= self._config.max_restart_attempts
                ),
                "last_verdict": state.last_verdict,
                "last_detail": state.last_detail,
            }
        return units

    async def _loop(self) -> None:
        # quiesce-exempt: probes service health endpoints and process state; no database
        while True:
            await asyncio.sleep(self._config.interval_s)
            await self.run_round()

    # ── one unit ─────────────────────────────────────────────────────────────

    def _resolvable_probe(self, unit_id: str, state: UnitHealth) -> Probe | None:
        """Resolve the unit's probe, or report the no-verdict case and answer None."""
        try:
            return self._registry.resolve(unit_id)
        except ProbeError as exc:
            state.last_verdict = "unavailable"
            state.last_detail = str(exc)
            _log.error(
                "[health] unit %s: no resolvable probe (%s) — no verdict, no action",
                unit_id,
                exc,
            )
            return None

    async def _check_unit(self, unit_id: str) -> None:
        state = self._unit_state(unit_id)
        probe = self._resolvable_probe(unit_id, state)
        if probe is None:
            return
        result = await self._probe(unit_id, probe)
        state.last_verdict = result.verdict.value
        state.last_detail = result.detail

        if result.alive:
            self._reset(state)
            _log.debug("[health] unit %s: alive (%s)", unit_id, result.detail)
            return

        deferral = self._deferral(unit_id)
        if deferral == _HELD_DOWN:
            # Operator stop: expected state, not a failure — no action, no
            # counting, no failure signal (the operator's own stop must not become noise).
            # The verdict still lands in `last_verdict`/`last_detail` for the
            # status and event surfaces.
            self._reset(state)
            _log.debug(
                "[health] unit %s: %s (%s) — expected (operator stop); no action",
                unit_id,
                result.verdict.value,
                result.detail,
            )
            return

        if result.terminal:
            self._report_terminal(unit_id, result.detail)
            return

        startup_remaining = self._startup_remaining(unit_id, state)
        if startup_remaining > 0:
            _log.info(
                "[health] unit %s: %s (%s); startup has %.1fs remaining — observation only",
                unit_id,
                result.verdict.value,
                result.detail,
                startup_remaining,
            )
            return

        if self._gate is not None:
            allowed, why = self._gate()
            if not allowed:
                _log.info(
                    "[health] unit %s: down (%s) but %s — not restarting this round",
                    unit_id,
                    result.detail,
                    why,
                )
                return
        if deferral is not None:
            self._report_terminal(unit_id, f"{result.detail}; restart deferred: {deferral}")
            return
        if state.restart_attempts >= self._config.max_restart_attempts:
            return  # The per-round failure-state signal reports exhausted attempts.
        state.restart_attempts += 1
        _log.warning(
            "[health] unit %s: down (%s), restarting (%d/%d)",
            unit_id,
            result.detail,
            state.restart_attempts,
            self._config.max_restart_attempts,
        )
        await self._supervisor.restart(unit_id)
        # Spawn acceptance is not readiness. The next ordinary round observes it.

    @staticmethod
    def _report_terminal(unit_id: str, detail: str) -> None:
        """Report a non-restartable observation without replenishing the budget."""
        from base.log import logger

        logger.warning(
            "[health] unit {unit}: NOT REVIVABLE by this unit ({detail}) — not restarting; "
            "resolve the reported failure before retrying",
            event="root_unit_not_revivable",
            unit=unit_id,
            detail=detail,
        )

    def _deferral(self, unit_id: str) -> str | None:
        """Probe-only units are observation targets, with no restart authority."""
        try:
            return self._supervisor.revival_deferral(unit_id)
        except UnknownUnitError:
            return _NO_REVIVAL_VERB

    async def _probe(self, unit_id: str, probe: Probe) -> DaemonProbe:
        """Inspection failures are unavailable evidence, never permission to restart."""
        if self._closed:
            raise RuntimeError("root health admission closed")
        runner = self._runners.setdefault(unit_id, ProbeRunner())
        before = self._generation(unit_id)
        result = await runner.observe(probe, self._config.probe_timeout_s)
        after = self._generation(unit_id)
        state = self._unit_state(unit_id)
        if state.generation != after:
            state.generation = after
            state.generation_ready = False
        if before != after:
            return DaemonProbe.unavailable("service generation changed during observation")
        if result.alive:
            state.generation_ready = True
        return result

    def _generation(self, unit_id: str) -> tuple[OwnedProcess, float] | None:
        try:
            return self._supervisor.health_generation(unit_id)
        except UnknownUnitError:
            return None  # Static observers have no startup generation or revival verb.

    def _startup_remaining(self, unit_id: str, state: UnitHealth) -> float:
        if state.generation is None or state.generation_ready:
            return 0.0
        grace = self._startup_graces.get(unit_id, self._config.startup_grace_s)
        return max(0.0, state.generation[1] + grace - _monotonic())

    def _unit_state(self, unit_id: str) -> UnitHealth:
        state = self._units.get(unit_id)
        if state is None:
            state = UnitHealth()
            self._units[unit_id] = state
        return state

    @staticmethod
    def _reset(state: UnitHealth) -> None:
        """A healthy observation or expected stop ends the continuous outage."""
        state.restart_attempts = 0
