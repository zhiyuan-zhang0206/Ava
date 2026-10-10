"""Read-only host diagnostics and one completed-round signal for root health.

A diagnostic never receives a supervisor or a launch verb. Its report hook may
record an existing alert, but cannot turn a failed observation into recovery.
Deadlines preserve UNAVAILABLE, and retained workers bound concurrency even when
native code ignores its own timeout.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from collections.abc import Callable, Sequence
from contextlib import suppress
from dataclasses import dataclass

from base.daemon.health import DaemonProbe, ProbeVerdict
from base.paths import ava_home
from services.supervision.ava_root.health import HealthMonitor, ProbeRunner

_log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class Diagnostic:
    name: str
    probe: Callable[[], DaemonProbe]
    interval_s: float = 60.0
    timeout_s: float = 20.0
    report: Callable[[DaemonProbe], None] | None = None

    def __post_init__(self) -> None:
        if self.interval_s <= 0 or self.timeout_s <= 0:
            raise ValueError("diagnostic timing must be positive")


@dataclass(slots=True)
class _State:
    """One diagnostic's sampling state; ``failures`` counts consecutive non-alive rounds."""

    runner: ProbeRunner
    next_due: float = 0.0
    sampled_at: float | None = None
    result: DaemonProbe | None = None
    failures: int = 0


class DiagnosticMonitor:
    """Finite diagnostic roster; one worker per entry and no autonomous repairs."""

    def __init__(self, diagnostics: Sequence[Diagnostic]) -> None:
        self._checks = tuple(diagnostics)
        if len({check.name for check in self._checks}) != len(self._checks):
            raise ValueError("duplicate diagnostic name")
        self._states = {check.name: _State(ProbeRunner()) for check in self._checks}
        self.unfinished_probes: tuple[str, ...] = ()
        self._closed = False

    async def stop(self) -> None:
        """Close the finite roster and join its native workers within one budget."""
        self._closed = True
        for state in self._states.values():
            state.runner.close()
        deadline = time.monotonic() + 0.2
        unfinished: list[str] = []
        failures: list[BaseException] = []
        for name, state in self._states.items():
            try:
                if not state.runner.stop(max(0.0, deadline - time.monotonic())):
                    unfinished.append(name)
            except BaseException as exc:
                failures.append(exc)
        self.unfinished_probes = tuple(unfinished)
        if unfinished:
            _log.warning("root diagnostics stopped with unfinished observations: %s", unfinished)
        if failures:
            raise BaseExceptionGroup("root diagnostic teardown failed", failures)

    async def run_round(self) -> None:
        if self._closed:
            raise RuntimeError("root diagnostic admission closed")
        await asyncio.gather(*(self._check(check) for check in self._checks))

    async def _check(self, check: Diagnostic) -> None:
        state = self._states[check.name]
        if time.monotonic() < state.next_due:
            return
        state.next_due = time.monotonic() + check.interval_s
        result = await state.runner.observe(check.probe, check.timeout_s)
        state.result = result
        state.sampled_at = time.time()
        recovered = result.alive and state.failures > 0
        state.failures = state.failures + 1 if not result.alive else 0
        self._report_sample(check, state, result, recovered=recovered)
        if check.report is not None and result.verdict != ProbeVerdict.UNAVAILABLE:
            # The result is fresh. Reporting is bounded through the SAME runner,
            # so an alert backend that hangs also prevents overlapping samples.
            reporter = check.report

            def report() -> DaemonProbe:
                reporter(result)
                return DaemonProbe.up("diagnostic alert recorded")

            reported = await state.runner.observe(report, check.timeout_s)
            if not reported.alive:
                _log.error("[diagnostic] %s reporting unavailable: %s", check.name, reported.detail)

    @staticmethod
    def _report_sample(
        check: Diagnostic, state: _State, result: DaemonProbe, *, recovered: bool
    ) -> None:
        """One `root_diagnostic` per non-alive sample, plus one on recovery.

        A condition is a state, so every failing sample (an unknown result
        included) reports at WARNING; the observability rule's pending period
        carries the debounce, and the first healthy sample after a streak logs
        the recovery.
        """
        if result.alive and not recovered:
            return
        from base.log import logger

        logger.log(
            "INFO" if result.alive else "WARNING",
            "root diagnostic {diagnostic}: {verdict} ({detail})",
            event="root_diagnostic",
            diagnostic=check.name,
            verdict=result.verdict.value,
            detail=result.detail,
            consecutive_failures=state.failures,
        )

    def health_snapshot(self) -> dict[str, object]:
        return {
            f"diagnostic:{check.name}": {
                "expected": True,
                "sampled_at": state.sampled_at,
                "last_verdict": None if state.result is None else state.result.verdict.value,
                "last_detail": "awaiting first sample"
                if state.result is None
                else state.result.detail,
                "consecutive_failures": state.failures,
            }
            for check in self._checks
            for state in (self._states[check.name],)
        }


class RootHealthRounds:
    """Own one cadence for service recovery and diagnostics, plus truthful freshness."""

    def __init__(
        self,
        health: HealthMonitor,
        diagnostics: DiagnosticMonitor,
        *,
        interval_s: float = 60.0,
        tasks: asyncio.TaskGroup | None = None,
    ) -> None:
        if interval_s <= 0:
            raise ValueError("root health interval must be positive")
        self._home = str(ava_home().resolve())
        self._home_id = hashlib.sha256(self._home.encode()).hexdigest()
        self._health = health
        self._diagnostics = diagnostics
        self._interval_s = interval_s
        self._task: asyncio.Task[None] | None = None
        self._tasks = tasks
        self._expected_since: float | None = None
        self._last_completed: float | None = None

    def health_snapshot(self) -> dict[str, object]:
        return (
            self._health.health_snapshot()
            | self._diagnostics.health_snapshot()
            | {
                "observer:root-health": {
                    "expected_since": self._expected_since,
                    "last_completed_at": self._last_completed,
                }
            }
        )

    async def run_round(self) -> None:
        await asyncio.gather(self._health.run_round(), self._diagnostics.run_round())
        from base.log import logger

        self._last_completed = time.time()
        logger.info(
            "root completed a health observation round",
            event="root_health_tick",
            home=self._home,
            home_id=self._home_id,
            last_tick_timestamp_seconds=self._last_completed,
        )

    async def start(self) -> None:
        if self._task is not None:
            raise RuntimeError("root health rounds already started")
        if self._tasks is None:
            raise RuntimeError("root health rounds require the participant task group")
        from base.log import init_gateway_process, logger

        init_gateway_process(name="ava-root")
        self._expected_since = time.time()
        logger.info(
            "root health rounds expected",
            event="root_health_expected",
            home=self._home,
            home_id=self._home_id,
            expected_since_timestamp_seconds=self._expected_since,
        )
        self._task = self._tasks.create_task(self._loop())

    async def _loop(self) -> None:
        # quiesce-exempt: samples process diagnostics; no database
        while True:
            await self.run_round()
            await asyncio.sleep(self._interval_s)

    async def stop(self) -> None:
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
        for owner in (self._health, self._diagnostics):
            try:
                await owner.stop()
            except BaseException as exc:
                failures.append(exc)
        if task is not None:
            from base.log import logger

            logger.info(
                "root health rounds intentionally stopped",
                event="root_health_expected",
                home=self._home,
                home_id=self._home_id,
                expected_since_timestamp_seconds=0.0,
            )
            self._expected_since = None
            from base import telemetry

            try:
                await asyncio.to_thread(telemetry.sync, timeout=2, bounded=True)
            except BaseException as exc:
                failures.append(exc)
        if failures:
            raise BaseExceptionGroup("root health rounds teardown failed", failures)
