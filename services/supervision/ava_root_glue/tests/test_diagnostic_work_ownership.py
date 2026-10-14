"""Diagnostic owners preserve bounded native work and honest process exit."""

from __future__ import annotations

import asyncio
import json
import sys
from contextlib import suppress
from threading import Event, Thread, current_thread
from time import monotonic

import pytest

from base.daemon.health import DaemonProbe
from services.supervision.ava_root_glue.diagnostics import Diagnostic, DiagnosticMonitor


async def _eventually(event: Event) -> None:
    deadline = monotonic() + 2
    while not event.is_set() and monotonic() < deadline:
        await asyncio.sleep(0.001)
    assert event.is_set()


async def _joined(worker: Thread) -> None:
    await asyncio.to_thread(worker.join, 1)
    assert not worker.is_alive(), "released diagnostic worker did not retire"


@pytest.mark.parametrize("late", [DaemonProbe.up("late green"), DaemonProbe.down("late red")])
async def test_probe_deadline_discards_late_report_and_allows_a_fresh_round(
    late: DaemonProbe,
) -> None:
    entered, release = Event(), Event()
    workers: list[Thread] = []
    reported: list[DaemonProbe] = []

    def probe() -> DaemonProbe:
        if workers:
            return DaemonProbe.up("fresh sample")
        workers.append(current_thread())
        entered.set()
        release.wait(3)
        return late

    monitor = DiagnosticMonitor(
        [Diagnostic("held", probe, interval_s=0.001, timeout_s=0.01, report=reported.append)]
    )
    try:
        await monitor.run_round()
        assert entered.is_set()
        state = monitor.health_snapshot()["diagnostic:held"]
        assert isinstance(state, dict) and state["last_verdict"] == "unavailable"
        release.set()
        await _joined(workers[0])
        assert reported == []  # no stop: only the expired observation closes admission
        await monitor.run_round()
        assert [result.detail for result in reported] == ["fresh sample"]
    finally:
        release.set()
        await monitor.stop()


async def test_stop_closes_reporting_before_the_probe_deadline() -> None:
    entered, release = Event(), Event()
    workers: list[Thread] = []
    reported: list[DaemonProbe] = []

    def probe() -> DaemonProbe:
        workers.append(current_thread())
        entered.set()
        release.wait(3)
        return DaemonProbe.up("late green")

    monitor = DiagnosticMonitor([Diagnostic("held", probe, timeout_s=5, report=reported.append)])
    round_task = asyncio.create_task(monitor.run_round())
    try:
        await _eventually(entered)
        await monitor.stop()
        assert monitor.unfinished_probes == ("held",)
        release.set()
        await _joined(workers[0])
        await round_task
        assert reported == []
    finally:
        release.set()
        await round_task
        await monitor.stop()


async def test_cancelled_round_closes_reporting_without_stopping_the_monitor() -> None:
    entered, release = Event(), Event()
    workers: list[Thread] = []
    reported: list[DaemonProbe] = []

    def probe() -> DaemonProbe:
        if workers:
            return DaemonProbe.up("fresh sample")
        workers.append(current_thread())
        entered.set()
        release.wait(3)
        return DaemonProbe.down("abandoned failure")

    monitor = DiagnosticMonitor(
        [Diagnostic("held", probe, interval_s=0.001, timeout_s=5, report=reported.append)]
    )
    round_task = asyncio.create_task(monitor.run_round())
    try:
        await _eventually(entered)
        round_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await round_task
        release.set()
        await _joined(workers[0])
        assert reported == []  # before deadline, and monitor.stop has not run
        await asyncio.sleep(0.002)
        await monitor.run_round()
        assert [result.detail for result in reported] == ["fresh sample"]
    finally:
        release.set()
        round_task.cancel()
        with suppress(asyncio.CancelledError):
            await round_task
        await monitor.stop()


async def test_stop_keeps_an_admitted_report_in_its_worker_without_waiting_for_it() -> None:
    entered, release = Event(), Event()
    workers: list[Thread] = []
    completed: list[DaemonProbe] = []

    def report(result: DaemonProbe) -> None:
        workers.append(current_thread())
        entered.set()
        release.wait(3)
        completed.append(result)

    monitor = DiagnosticMonitor(
        [Diagnostic("held", lambda: DaemonProbe.up("fresh sample"), timeout_s=5, report=report)]
    )
    round_task = asyncio.create_task(monitor.run_round())
    try:
        await _eventually(entered)
        started = monotonic()
        await monitor.stop()
        assert monotonic() - started < 1, "report callback held the stop admission lock"
        assert monitor.unfinished_probes == ("held",)
        assert completed == []
        release.set()
        await _joined(workers[0])
        await round_task
        assert [result.detail for result in completed] == ["fresh sample"]
    finally:
        release.set()
        await round_task
        await monitor.stop()


async def test_stopped_diagnostic_reports_live_worker_and_closes_admission() -> None:
    release, completed = Event(), Event()
    calls = 0

    def blocked() -> DaemonProbe:
        nonlocal calls
        calls += 1
        try:
            release.wait(3)
            return DaemonProbe.up("late green")
        finally:
            completed.set()

    monitor = DiagnosticMonitor([Diagnostic("blocked", blocked, timeout_s=0.01)])
    await monitor.run_round()
    try:
        stopped = asyncio.create_task(monitor.stop())
        done, _ = await asyncio.wait({stopped}, timeout=1)
        assert stopped in done, "native work held diagnostic stop past its independent guard"
        await stopped
        assert monitor.unfinished_probes == ("blocked",)
        assert calls == 1
        with pytest.raises(RuntimeError, match="admission closed"):
            await monitor._states["blocked"].runner.observe(blocked, 0.01)
    finally:
        release.set()
        await _eventually(completed)
        await monitor.stop()
    assert monitor.unfinished_probes == ()


async def test_process_exits_with_honestly_unfinished_native_observation() -> None:
    code = """
import asyncio,json,threading
from base.daemon.health import DaemonProbe
from services.supervision.ava_root_glue.diagnostics import Diagnostic,DiagnosticMonitor
release = threading.Event()
def blocked():
    release.wait(60)
    return DaemonProbe.up('late green')
async def run():
    monitor = DiagnosticMonitor([Diagnostic('blocked',blocked,timeout_s=.01)])
    await monitor.run_round()
    await monitor.stop()
    print(json.dumps({'unfinished':monitor.unfinished_probes,'live_workers':sum(t.name == 'root-probe' for t in threading.enumerate())}))
asyncio.run(run())
"""
    child = await asyncio.create_subprocess_exec(
        sys.executable,
        "-I",
        "-c",
        code,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    output = asyncio.create_task(child.communicate())
    try:
        done, _ = await asyncio.wait({output}, timeout=5)
        assert output in done, "the process could not exit with its bounded daemon observation"
        stdout, stderr = await output
        assert child.returncode == 0, stderr.decode()
        assert json.loads(stdout) == {"unfinished": ["blocked"], "live_workers": 1}
    finally:
        if child.returncode is None:
            child.kill()
        await output
