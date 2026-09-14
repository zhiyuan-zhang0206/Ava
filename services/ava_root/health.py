"""The health runner: one probe round over every unit, revival included.

This is the root-side home of what the per-capability watchdog daemons do today:
probe a unit; when it is down, act once — restart it and let the probe, never
the act of restarting, decide whether the restart worked. Vocabulary and policy
are inherited, not re-invented:

- **Verdicts** are `shared.daemon_health.DaemonProbe` (`alive` / `down` /
  terminal `port-taken` / `unavailable`) — the same total probe the operator
  surfaces already trust. A probe that raises is wrapped into `down`
  (fail-closed); a probe that cannot be resolved yields NO action at all.
- **Policy** mirrors `shared.service_respawn.run_keepalive`: a non-alive round
  counts, the restart action is gated by a consecutive-failure threshold, a
  restart that failed to verify is spaced by exponential backoff, and after
  `breaker_rounds` consecutive non-alive rounds the breaker holds — one
  WARNING, then a per-round hold alert; any probe-alive round resets everything.
- **The restart verb** is the supervisor's subtree replacement
  (`Supervisor.restart`); the verb's return only says the replacement was
  accepted, so the round re-probes until the unit confirms — exactly why
  `respawn_and_verify` exists.
- **Deferral** (`Supervisor.revival_deferral`) keeps the two revivers from
  fighting: a round asks why it must not act. An operator stop is an expected
  state — no action, no counting, no alert. Every other reason suppresses the
  ACTION only: the down verdict still counts toward the breaker, so a long-dead
  unit still raises one alert and holds — never a silent permanent defer.

The monitor is mechanism only: nothing in the daemon builds one yet, and it is
platform-neutral — no concrete process manager is ever named here.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass, replace
from typing import Protocol

from services.ava_root.probes import Probe, ProbeError, ProbeRegistry
from shared.daemon_health import DaemonProbe

_log = logging.getLogger(__name__)


def _monotonic() -> float:
    """Wall-independent clock for the backoff deadlines — a module seam so tests
    can advance time (the same pattern as `shared.service_respawn`)."""
    return time.monotonic()


class RevivalHost(Protocol):
    """The supervisor slice the health runner needs (duck-typed for stubs)."""

    async def restart(self, unit_id: str) -> dict[str, object]:
        """Subtree-replace `unit_id`'s generation (start-new-before-stop-old)."""
        ...

    def revival_deferral(self, unit_id: str) -> str | None:
        """Why a would-be reviver must not act on `unit_id` now, or None."""
        ...


HealthGate = Callable[[], tuple[bool, str]]
"""A blocking-policy seam: `(allowed, why)`; absent means always allowed.

The watchdog's ops-reconcile gating is deliberately NOT absorbed here — this is
the generic no-argument interface a later slice can plug into, empty by default.
"""


@dataclass(frozen=True, slots=True)
class HealthConfig:
    """Timing policy for the health runner (seconds unless noted)."""

    interval_s: float = 60.0
    """Round period — the watchdog's 60s cadence."""

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
        if self.interval_s <= 0:
            raise ValueError("interval_s must be positive")
        if self.verify_interval_s <= 0:
            raise ValueError("verify_interval_s must be positive")
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


class HealthMonitor:
    """One probe round over every registered unit, acting per policy.

    Rounds run sequentially — one round is a bounded stream of probes — and each
    unit is isolated: a probe failure, resolution error, or restart error in one
    unit never aborts the round for the others. Per-unit state lives in this
    process, exactly like the watchdog counters; a fresh process starts without
    history.
    """

    def __init__(
        self,
        supervisor: RevivalHost,
        registry: ProbeRegistry,
        *,
        config: HealthConfig | None = None,
        gate: HealthGate | None = None,
    ) -> None:
        self._supervisor = supervisor
        self._registry = registry
        self._config = config if config is not None else HealthConfig()
        self._gate = gate
        self._units: dict[str, UnitHealth] = {}
        self._task: asyncio.Task[None] | None = None

    async def run_round(self) -> None:
        """Probe every registered unit once, in registration order."""
        for unit_id in self._registry.unit_ids():
            try:
                await self._check_unit(unit_id)
            except Exception:
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

        deferral = self._supervisor.revival_deferral(unit_id)
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

        failures = state.consecutive_failures + 1
        state.consecutive_failures = failures
        if failures >= self._config.breaker_rounds and state.breaker_since is None:
            state.breaker_since = _monotonic()
            # One alert per hold episode; the per-round hold line carries the state.
            _log.warning(
                "[health] unit %s: restart breaker OPEN after %d rounds without a live "
                "probe (%s) — holding restarts; manual intervention needed%s",
                unit_id,
                failures,
                result.detail,
                deferred_note,
            )
        if state.breaker_since is not None:
            _log.warning(
                "[health] unit %s: down, restart held for %.0fs (%s) — not restarting%s",
                unit_id,
                _monotonic() - state.breaker_since,
                result.detail,
                deferred_note,
            )
            return
        if failures < self._config.failures_before_restart:
            _log.warning(
                "[health] unit %s: probe failed (%d/%d) — not restarting yet",
                unit_id,
                failures,
                self._config.failures_before_restart,
            )
            return
        backoff = self._backoff_remaining(state)
        if backoff > 0:
            _log.warning(
                "[health] unit %s: down (%s) — backing off after a failed restart; "
                "next attempt in %ds",
                unit_id,
                result.detail,
                int(backoff),
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
        _log.warning(
            "[health] unit %s: restart FAILED (%s) — next attempt in %ds",
            unit_id,
            after.detail,
            int(delay_s),
        )

    async def _probe(self, unit_id: str, probe: Probe) -> DaemonProbe:
        """Run one probe off the event loop; a raise is wrapped into `down`."""
        try:
            return await asyncio.to_thread(probe)
        except Exception as exc:
            _log.exception("[health] unit %s: probe raised; treating as down", unit_id)
            return DaemonProbe.down(f"probe raised {type(exc).__name__}: {exc}")

    async def _restart_and_verify(
        self, unit_id: str, state: UnitHealth, probe: Probe
    ) -> tuple[DaemonProbe, float]:
        """Subtree-replace the unit, then poll the probe until it confirms.

        Returns the last verdict and the backoff delay armed for the next
        attempt. A terminal verdict ends the poll immediately (the occupant will
        not yield — the same rule as `respawn_and_verify`); the deadline bounds
        the rest.
        """
        delay_s = min(
            self._config.backoff_base_s * (2 ** (state.respawn_attempts - 1)),
            self._config.backoff_cap_s,
        )
        state.next_respawn_at = _monotonic() + delay_s
        await self._supervisor.restart(unit_id)
        # The verb's return says "replacement accepted", never "serving" — only
        # the probe decides what happened (the respawn_and_verify doctrine).
        result = await self._probe(unit_id, probe)
        deadline = _monotonic() + self._config.verify_deadline_s
        while not result.alive and not result.terminal and _monotonic() < deadline:
            await asyncio.sleep(self._config.verify_interval_s)
            result = await self._probe(unit_id, probe)
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
