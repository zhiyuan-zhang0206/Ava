"""The scan loop's stall-alert gate: an open deploy window — rechecked on its
own cadence — and the settle window after it closes (or after a boot / a
stop-window resume) hold stall alerts back while the wake re-dispatch and the
dead-letter sweeps keep running. `test_delivery_watchdog.py` is at its frozen
ceiling, so these loop-level gate tests live here."""

from __future__ import annotations

import asyncio
from typing import cast
from unittest.mock import MagicMock

import pytest
from psycopg_pool import ConnectionPool

from base.config import settings
from base.daemon import round_loop
from base.daemon.loop_health import LoopProgress
from base.db import Database
from base.deploy.maintenance import admission
from base.events.live.bus import EventBus
from ops.deploy_window import DeployWindow
from services.delivery_watchdog import daemon

_OPEN = DeployWindow(active=True, detail="machine 'macmini' is mid-deploy (posture=stop)")
_IDLE = DeployWindow(active=False, detail="no deploy in flight")


class _LoopHarness:
    """`_scan_loop` with the alert half spied and every database seam faked:
    `calls` records "dispatch"/"reload"/"scan" as the loop reaches each seam,
    and `window`/`quiesced` are driven by the test while the loop runs."""

    def __init__(
        self,
        monkeypatch: pytest.MonkeyPatch,
        *,
        window: list[DeployWindow],
        grace_s: float,
    ) -> None:
        self.calls: list[str] = []
        self.window = window
        self.quiesced = False
        monkeypatch.setattr(settings.daemon, "delivery_watchdog_alert_grace_seconds", grace_s)
        monkeypatch.setattr(daemon, "_DEPLOY_WINDOW_RECHECK_S", 0.001)
        monkeypatch.setattr(admission, "quiesced", lambda: self.quiesced)

        def read_window(_db: Database) -> DeployWindow:
            return self.window[0]

        def dispatch(*_a: object, **_kw: object) -> int:
            self.calls.append("dispatch")
            return 0

        def reload(_pool: ConnectionPool) -> set[int]:
            self.calls.append("reload")
            return set()

        def scan(
            _pool: ConnectionPool, _threshold: float, alerted: set[int]
        ) -> tuple[int, set[int]]:
            self.calls.append("scan")
            return 0, alerted

        def noop(*_a: object, **_kw: object) -> None:
            return None

        def gc(*_a: object, **_kw: object) -> int:
            return 0

        def sweep(*_a: object, **_kw: object) -> float:
            return 0.0

        monkeypatch.setattr(daemon, "deploy_in_flight", read_window)
        monkeypatch.setattr(daemon, "dispatch_wakes", dispatch)
        monkeypatch.setattr(daemon, "select_alerted_ids", reload)
        monkeypatch.setattr(daemon, "scan_once", scan)
        monkeypatch.setattr(daemon, "persist_alerted", noop)
        monkeypatch.setattr(daemon, "prune_alerted", noop)
        monkeypatch.setattr(daemon, "gc_alerted", gc)
        monkeypatch.setattr(daemon, "_maybe_sweep_stale_inbounds", sweep)

        async def short_sleep(_progress: LoopProgress, _total_s: float) -> None:
            await asyncio.sleep(0.005)

        monkeypatch.setattr(round_loop, "sleep_with_progress", short_sleep)

    def start(self) -> asyncio.Task[None]:
        return asyncio.create_task(
            daemon._scan_loop(
                cast("ConnectionPool", MagicMock()),
                Database.from_settings(),
                cast("EventBus", MagicMock()),
                LoopProgress("test", 60.0),
            )
        )


async def _stop(task: asyncio.Task[None]) -> None:
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


async def test_alerts_hold_while_a_deploy_window_is_open(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _LoopHarness(monkeypatch, window=[_OPEN], grace_s=0.0)
    task = harness.start()
    try:
        await asyncio.sleep(0.1)
    finally:
        await _stop(task)
    assert "scan" not in harness.calls
    assert "dispatch" in harness.calls  # the window holds alerts, not the queue drain


async def test_alerts_resume_after_the_window_and_its_settle_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _LoopHarness(monkeypatch, window=[_OPEN], grace_s=0.25)
    task = harness.start()
    try:
        await asyncio.sleep(0.05)
        assert "scan" not in harness.calls
        harness.window[0] = _IDLE
        await asyncio.sleep(0.1)  # inside the settle window
        assert "scan" not in harness.calls
        await asyncio.sleep(0.3)  # past it
    finally:
        await _stop(task)
    assert "scan" in harness.calls


async def test_boot_holds_alerts_through_the_settle_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A daemon (re)start is lifecycle-adjacent: its first settle window is the
    same resume tail a closing deploy window opens."""
    harness = _LoopHarness(monkeypatch, window=[_IDLE], grace_s=0.25)
    task = harness.start()
    try:
        await asyncio.sleep(0.1)
        assert "scan" not in harness.calls
        await asyncio.sleep(0.3)
    finally:
        await _stop(task)
    assert "scan" in harness.calls


async def test_a_stop_window_resume_reenters_through_a_fresh_settle_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _LoopHarness(monkeypatch, window=[_IDLE], grace_s=0.25)
    task = harness.start()
    try:
        await asyncio.sleep(0.4)  # past the boot grace: alerts run
        assert "scan" in harness.calls
        harness.quiesced = True
        await asyncio.sleep(0.1)  # nothing scans inside the stop window
        harness.calls.clear()
        harness.quiesced = False
        await asyncio.sleep(0.1)  # inside the resume's fresh settle window
        assert "scan" not in harness.calls
        await asyncio.sleep(0.3)
    finally:
        await _stop(task)
    assert "scan" in harness.calls
