"""Diagnostic owners preserve bounded native work and honest process exit."""

from __future__ import annotations

import asyncio
import json
import sys
from threading import Event
from time import monotonic

import pytest

from base.daemon.health import DaemonProbe
from services.supervision.ava_root_glue.diagnostics import Diagnostic, DiagnosticMonitor


async def _eventually(event: Event) -> None:
    deadline = monotonic() + 2
    while not event.is_set() and monotonic() < deadline:
        await asyncio.sleep(0.001)
    assert event.is_set()


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
