"""Root-owned service health, with bounded observation and verified recovery.

Only observed DOWN results can request replacement through the supervisor.
Unknown identity, inspection errors, timeouts, and foreign listeners are terminal
observations until fresh evidence resolves them. The supervisor retains native
custody across failed stops; health is the sole service retry scheduler.
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
from threading import Thread
from typing import Protocol

from services.ava_root.manifest import UnknownUnitError
from services.ava_root.probes import Probe, ProbeError, ProbeRegistry
from shared.daemon_health import DaemonProbe
from shared.proc_tree import OwnedProcess

_log = logging.getLogger(__name__)


def _monotonic() -> float:
    """Wall-independent backoff clock, replaceable in tests."""
    return time.monotonic()


class RevivalHost(Protocol):
    """The supervisor slice the health runner needs (duck-typed for stubs)."""

    async def restart(self, unit_id: str) -> dict[str, object]:
        """Replace a generation only after the supervisor settles native custody."""
        ...

    def health_generation(self, unit_id: str) -> tuple[OwnedProcess, float] | None:
        """Retained native birth and monotonic start; never recaptured from a PID."""
        ...

    def revival_deferral(self, unit_id: str) -> str | None:
        """Why a would-be reviver must not act on `unit_id` now, or None."""
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

    verify_deadline_s: float = 20.0
    """How long a restart's confirmation poll may run, probe-inclusive."""

    verify_interval_s: float = 0.5
    """Spacing between confirmation polls inside that window."""

    failures_before_restart: int = 1
    """Consecutive non-alive rounds required before the restart action."""

    backoff_base_s: float = 60.0
    """First delay armed after a restart that failed to verify; doubles per try."""

    backoff_cap_s: float = 1800.0
    """Ceiling for that delay."""

    breaker_rounds: int = 5
    """Consecutive non-alive rounds that open the hold-forever breaker."""

    def __post_init__(self) -> None:
        if not math.isfinite(self.startup_grace_s) or self.startup_grace_s < 0:
            raise ValueError("startup_grace_s must be nonnegative")
        if self.probe_timeout_s <= 0:
            raise ValueError("probe_timeout_s must be positive")
        if self.interval_s <= 0:
            raise ValueError("interval_s must be positive")
        if self.verify_interval_s <= 0:
            raise ValueError("verify_interval_s must be positive")
        if self.verify_deadline_s <= 0:
            raise ValueError("verify_deadline_s must be positive")
        if self.backoff_base_s <= 0:
            raise ValueError("backoff_base_s must be positive")
        if self.backoff_cap_s < self.backoff_base_s:
            raise ValueError("backoff_cap_s must be at least backoff_base_s")
        if self.failures_before_restart < 1:
            raise ValueError("failures_before_restart must be at least 1")
        if self.breaker_rounds <= self.failures_before_restart:
            # The breaker check runs before the threshold branch: an equal value
            # would open the breaker on the very round the threshold is met and
            # hold without a single restart attempt (same rule as run_keepalive).
            raise ValueError("breaker_rounds must be greater than failures_before_restart")


@dataclass(slots=True)
class UnitHealth:
    """One unit's accumulated health state — the runner's whole memory of it."""

    generation: tuple[OwnedProcess, float] | None = None
    """Retained native generation to which the latest observation belongs."""

    generation_ready: bool = False
    """Whether this generation has produced a fresh ALIVE observation."""

    consecutive_failures: int = 0
    """Counted non-alive rounds since the last reset; expected stops do not count."""

    respawn_attempts: int = 0
    """Restart attempts since the last reset — the backoff exponent."""

    next_respawn_at: float | None = None
    """Monotonic deadline before the next restart is due (armed per attempt)."""

    breaker_since: float | None = None
    """Monotonic time the breaker opened; None while it is closed."""

    last_verdict: str | None = None
    """Value of the last probe verdict (`alive` / `down` / `port-taken` / `unavailable`)."""

    last_detail: str = ""
    """Detail string of the last probe verdict."""


# The supervisor's deferral reason for a unit the operator holds down
# (`desired != RUNNING`). That state is EXPECTED, never a failure: it is the one
# reason a down verdict neither counts toward the breaker nor alerts. Every
# other reason (an in-flight retry, a `never` policy) suppresses the action only.
_HELD_DOWN = "held down"

# The reason held for a unit that is not in this tree at all (the static probe
# path — a deployment-registered observer outside the supervisor's registry,
# task #3393): the runner reports it but has no restart verb to reach for.
_NO_REVIVAL_VERB = "no revival verb (not a tree unit)"


class ProbeRunner:
    """One bounded synchronous observation, with at most one daemon worker.

    Python cannot cancel a blocked native call. A deadline therefore reports
    UNAVAILABLE and retains the worker until it exits. Later rounds cannot
    replace it; its late result is discarded before a fresh observation starts.
    Daemon threads cannot hold root shutdown hostage.
    """

    def __init__(self) -> None:
        self._pending: Future[DaemonProbe] | None = None

    async def observe(self, probe: Probe, timeout_s: float) -> DaemonProbe:
        if self._pending is not None and not self._pending.done():
            return DaemonProbe.unavailable("previous observation still running after its deadline")
        pending: Future[DaemonProbe] = Future()
        self._pending = pending

        def run() -> None:
            try:
                result = probe()
                if not isinstance(result, DaemonProbe):
                    result = DaemonProbe.unavailable("probe did not return DaemonProbe")
            except Exception as exc:
                result = DaemonProbe.unavailable(f"probe raised {type(exc).__name__}: {exc}")
            pending.set_result(result)

        Thread(target=run, name="root-probe", daemon=True).start()
        try:
            result = await asyncio.wait_for(asyncio.shield(asyncio.wrap_future(pending)), timeout_s)
        except TimeoutError:
            return DaemonProbe.unavailable(f"observation exceeded {timeout_s:g}s deadline")
        self._pending = None
        return result


class HealthMonitor:
    """One probe round over every registered unit, acting per policy.

    Rounds run sequentially — one round is a bounded stream of probes — and each
    unit is isolated: a probe failure, resolution error, or restart error in one
    unit never aborts the round for the others. Per-unit state lives in this
    process; a fresh process starts without observation history.
    """

    def __init__(
        self,
        supervisor: RevivalHost,
        registry: ProbeRegistry,
        *,
        config: HealthConfig | None = None,
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

    async def run_round(self) -> None:
        """Probe every registered unit once, in registration order."""
        for unit_id in self._registry.unit_ids():
            try:
                await self._check_unit(unit_id)
            except Exception as exc:
                state = self._unit_state(unit_id)
                state.last_verdict = "unavailable"
                state.last_detail = f"health round raised {type(exc).__name__}: {exc}"
                _log.exception("[health] unit %s: round failed; continuing", unit_id)

    async def start(self) -> None:
        """Run rounds until `stop()` — sleep first, then one round per interval."""
        if self._task is not None:
            raise RuntimeError("health monitor already started")
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        """Stop the round loop; safe to call when not started."""
        task = self._task
        self._task = None
        if task is None:
            return
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task

    def snapshot(self) -> dict[str, UnitHealth]:
        """A copy of every known unit's health state, keyed by unit id."""
        return {unit_id: replace(state) for unit_id, state in self._units.items()}

    def health_snapshot(self) -> dict[str, object]:
        """A plain-dict view for `Supervisor.status()` (the B7 health surface)."""
        now = _monotonic()
        units: dict[str, object] = {}
        for unit_id, state in self._units.items():
            units[unit_id] = {
                "startup_remaining_s": self._startup_remaining(unit_id, state),
                "generation_ready": state.generation_ready,
                "consecutive_failures": state.consecutive_failures,
                "respawn_attempts": state.respawn_attempts,
                "breaker_open": state.breaker_since is not None,
                "breaker_for_s": (
                    None if state.breaker_since is None else max(0.0, now - state.breaker_since)
                ),
                "next_restart_in_s": (
                    None if state.next_respawn_at is None else self._backoff_remaining(state)
                ),
                "last_verdict": state.last_verdict,
                "last_detail": state.last_detail,
            }
        return units

    @staticmethod
    def _emit_breaker_open(unit_id: str, rounds: int, attempts: int, detail: str) -> None:
        """One registered alert per unresolved service failure episode."""
        from shared.log import logger

        logger.warning(
            "[health] unit {unit}: restart breaker OPEN after {rounds} rounds without a "
            "live probe ({detail}) — holding restarts; manual intervention needed",
            event="root_restart_breaker_open",
            unit=unit_id,
            rounds=rounds,
            respawn_attempts=attempts,
            detail=detail,
        )

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(self._config.interval_s)
            try:
                await self.run_round()
            except Exception:
                # `run_round` isolates per unit; this catches a defect in the
                # round itself. A dead loop would stop health checks silently,
                # so the loop survives its own bug — loudly.
                _log.exception("[health] round raised; continuing")

    # ── one unit ─────────────────────────────────────────────────────────────

    async def _check_unit(self, unit_id: str) -> None:
        state = self._unit_state(unit_id)
        try:
            probe = self._registry.resolve(unit_id)
        except ProbeError as exc:
            state.last_verdict = "unavailable"
            state.last_detail = str(exc)
            _log.error(
                "[health] unit %s: no resolvable probe (%s) — no verdict, no action",
                unit_id,
                exc,
            )
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
            # counting, no alert (the operator's own stop must not become noise).
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
            self._reset(state)
            _log.error(
                "[health] unit %s: NOT REVIVABLE by this unit (%s) — not restarting; "
                "resolve the reported ownership or inspection failure before retrying",
                unit_id,
                result.detail,
            )
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

        deferred_note = "" if deferral is None else f" — restart deferred: {deferral}"

        if self._gate is not None:
            allowed, why = self._gate()
            if not allowed:
                # A declined round is "not yet allowed", never "a restart cannot
                # cure it": state resets so the first open round can act
                # (issue #2101).
                self._reset(state)
                _log.info(
                    "[health] unit %s: down (%s) but %s — not restarting this round",
                    unit_id,
                    result.detail,
                    why,
                )
                return

        if not self._count_and_gate(unit_id, state, result.detail, deferred_note):
            return
        if deferral == _NO_REVIVAL_VERB:
            _log.info(
                "[health] unit %s: down (%s) — outside this tree (no revival verb); "
                "reporting only, not restarting",
                unit_id,
                result.detail,
            )
            return
        if deferral is not None:
            _log.info(
                "[health] unit %s: down (%s) — restart deferred (%s); not restarting this round",
                unit_id,
                result.detail,
                deferral,
            )
            return
        _log.info("[health] unit %s: down (%s), restarting...", unit_id, result.detail)
        state.respawn_attempts += 1
        after, delay_s = await self._restart_and_verify(unit_id, state, probe)
        state.last_verdict = after.verdict.value
        state.last_detail = after.detail
        if after.alive:
            self._reset(state)
            _log.info("[health] unit %s: restarted, verified alive (%s)", unit_id, after.detail)
            return
        if after.terminal:
            self._reset(state)
            _log.error(
                "[health] unit %s: restart cannot win (%s) — not retrying; resolve the "
                "reported ownership or inspection failure before retrying",
                unit_id,
                after.detail,
            )
            return
        self._report_unready_restart(unit_id, state, after.detail, delay_s)

    def _report_unready_restart(
        self, unit_id: str, state: UnitHealth, detail: str, delay_s: float
    ) -> None:
        startup_remaining = self._startup_remaining(unit_id, state)
        if startup_remaining > 0:
            _log.info(
                "[health] unit %s: replacement is initializing (%s); startup has %.1fs remaining",
                unit_id,
                detail,
                startup_remaining,
            )
            return
        _log.warning(
            "[health] unit %s: restart FAILED (%s) — next attempt in %ds",
            unit_id,
            detail,
            int(delay_s),
        )

    def _count_and_gate(
        self, unit_id: str, state: UnitHealth, detail: str, deferred_note: str
    ) -> bool:
        """Count the round, run the breaker, and answer whether to restart now.

        Same order and log lines as the original inline block: breaker check
        first, then the consecutive-failure threshold, then the post-restart
        backoff; the restart itself stays with the caller.
        """
        failures = state.consecutive_failures + 1
        state.consecutive_failures = failures
        if failures >= self._config.breaker_rounds and state.breaker_since is None:
            state.breaker_since = _monotonic()
            # One alert per hold episode — a registered event; the per-round
            # hold line below carries the continuing state.
            self._emit_breaker_open(unit_id, failures, state.respawn_attempts, detail)
        if state.breaker_since is not None:
            _log.warning(
                "[health] unit %s: down, restart held for %.0fs (%s) — not restarting%s",
                unit_id,
                _monotonic() - state.breaker_since,
                detail,
                deferred_note,
            )
            return False
        if failures < self._config.failures_before_restart:
            _log.warning(
                "[health] unit %s: probe failed (%d/%d) — not restarting yet",
                unit_id,
                failures,
                self._config.failures_before_restart,
            )
            return False
        backoff = self._backoff_remaining(state)
        if backoff > 0:
            _log.warning(
                "[health] unit %s: down (%s) — backing off after a failed restart; "
                "next attempt in %ds",
                unit_id,
                detail,
                int(backoff),
            )
            return False
        return True

    def _deferral(self, unit_id: str) -> str | None:
        """Why a would-be reviver must not act — with out-of-tree units folded in.

        A unit registered by probe only (the static path — a
        deployment-registered observer outside this registry, task #3393) is
        absent from the supervisor's registry, and `revival_deferral` raises
        `UnknownUnitError` for it. Nothing in this tree can revive it, so it
        holds under `_NO_REVIVAL_VERB` like any other suppressed action —
        counted toward the breaker, surfaced, never restarted.
        """
        try:
            return self._supervisor.revival_deferral(unit_id)
        except UnknownUnitError:
            return _NO_REVIVAL_VERB

    async def _probe(
        self, unit_id: str, probe: Probe, *, timeout_s: float | None = None
    ) -> DaemonProbe:
        """Inspection failures are unavailable evidence, never permission to restart."""
        runner = self._runners.setdefault(unit_id, ProbeRunner())
        budget = self._config.probe_timeout_s
        if timeout_s is not None:
            budget = min(budget, timeout_s)
        if budget <= 0:
            return DaemonProbe.unavailable("verification observation deadline exhausted")
        before = self._generation(unit_id)
        result = await runner.observe(probe, budget)
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

    async def _restart_and_verify(
        self, unit_id: str, state: UnitHealth, probe: Probe
    ) -> tuple[DaemonProbe, float]:
        """Subtree-replace the unit, then poll the probe until it confirms.

        Returns the last verdict and the backoff delay armed for the next
        attempt. A terminal verdict ends the poll immediately (the occupant will
        not yield); the deadline bounds
        the rest.
        """
        delay_s = min(
            self._config.backoff_base_s * (2 ** (state.respawn_attempts - 1)),
            self._config.backoff_cap_s,
        )
        state.next_respawn_at = _monotonic() + delay_s
        await self._supervisor.restart(unit_id)
        # The verb's return says "replacement accepted", never "serving" — only
        # the probe decides what happened (the readiness contract).
        deadline = _monotonic() + self._config.verify_deadline_s
        result = await self._probe(unit_id, probe, timeout_s=deadline - _monotonic())
        while not result.alive and not result.terminal and _monotonic() < deadline:
            await asyncio.sleep(
                min(self._config.verify_interval_s, max(0, deadline - _monotonic()))
            )
            if _monotonic() >= deadline:
                break
            result = await self._probe(unit_id, probe, timeout_s=deadline - _monotonic())
        return result, delay_s

    def _unit_state(self, unit_id: str) -> UnitHealth:
        state = self._units.get(unit_id)
        if state is None:
            state = UnitHealth()
            self._units[unit_id] = state
        return state

    @staticmethod
    def _reset(state: UnitHealth) -> None:
        """Full reset: failure count, backoff exponent + deadline, breaker."""
        state.consecutive_failures = 0
        state.respawn_attempts = 0
        state.next_respawn_at = None
        state.breaker_since = None

    @staticmethod
    def _backoff_remaining(state: UnitHealth) -> float:
        """Seconds until the next restart is due (0 when none is armed or it is due)."""
        if state.next_respawn_at is None:
            return 0.0
        return max(0.0, state.next_respawn_at - _monotonic())
