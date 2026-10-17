from __future__ import annotations

import asyncio
import logging
import os
import signal
import subprocess
import sys
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from typing import cast

import pytest

from base.log.tests.log_records import LogRecords
from base.log.tests.log_records import loguru_records as loguru_records
from services.supervision.meiva_root import selfcheck as selfcheck_mod
from services.supervision.meiva_root.manifest import RestartPolicy, UnitManifest, UnitRegistry
from services.supervision.meiva_root.selfcheck import (
    SelfCheckConfig,
    TreeSelfCheck,
)
from services.supervision.meiva_root.supervisor import Supervisor


class _Recorder:
    def __init__(self, records: LogRecords) -> None:
        self._records = records

    @property
    def calls(self) -> list[dict[str, object]]:
        return [{"message": r["message"], **r["extra"]} for r in self._records]

    def events(self, name: str) -> list[dict[str, object]]:
        return [c for c in self.calls if c.get("event") == name]


@pytest.fixture
def recorder(loguru_records: LogRecords) -> _Recorder:
    return _Recorder(loguru_records)


class _StubHost:
    def __init__(self, units: list[dict[str, object]], root_pid: int | None = None) -> None:
        self.units = units
        self.root_pid = os.getpid() if root_pid is None else root_pid

    def tree_view(self) -> dict[str, object]:
        return {"root_pid": self.root_pid, "units": self.units}


def _broken_unit(unit_id: str) -> dict[str, object]:
    return {"id": unit_id, "state": "running", "pid": 99999999}


def _spawn_sleeper() -> subprocess.Popen[bytes]:
    return subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])


def _chain(check: TreeSelfCheck) -> dict[str, object]:
    return cast("dict[str, object]", check.metrics_snapshot()["chain"])


def test_walk_checks_every_running_unit_and_skips_acknowledged_states(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG, logger="services.supervision.meiva_root.selfcheck")
    sleeper = _spawn_sleeper()
    try:
        host = _StubHost(
            [
                {"id": "parent", "state": "running", "pid": sleeper.pid},
                {"id": "child", "state": "running", "pid": sleeper.pid},
                {"id": "idle", "state": "stopped", "pid": None},
                {"id": "waiting", "state": "backoff", "pid": None},
            ]
        )
        check = TreeSelfCheck(host)
        check.run_once()
        chain = _chain(check)
        assert chain["broken"] is False
        assert chain["broken_units"] == []
        assert chain["rounds"] == 1
        assert "chain intact (4 unit(s) walked)" in caplog.text
    finally:
        sleeper.kill()
        sleeper.wait()


def _assert_first_episode_opened_one_event(check: TreeSelfCheck, recorder: _Recorder) -> None:
    chain = _chain(check)
    assert chain["broken"] is True
    assert chain["broken_total"] == 1
    assert chain["broken_units"] == ["svc"]
    assert chain["broken_for_s"] is not None
    events = recorder.events("root_chain_broken")
    assert len(events) == 1
    assert events[0]["units"] == ["svc"]
    assert cast("dict[str, object]", events[0]["reasons"]) == {"svc": "missing"}


def test_missing_process_opens_one_event_per_episode(
    recorder: _Recorder, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG, logger="services.supervision.meiva_root.selfcheck")
    host = _StubHost([_broken_unit("svc")])
    check = TreeSelfCheck(host)

    check.run_once()
    _assert_first_episode_opened_one_event(check, recorder)

    check.run_once()
    assert len(recorder.events("root_chain_broken")) == 1
    assert "still broken" in caplog.text

    host.units = []
    check.run_once()
    chain = _chain(check)
    assert chain["broken"] is False
    assert chain["broken_total"] == 1
    assert "intact again" in caplog.text

    host.units = [_broken_unit("svc")]
    check.run_once()
    assert len(recorder.events("root_chain_broken")) == 2
    assert _chain(check)["broken_total"] == 2


def test_detached_process_is_broken(recorder: _Recorder) -> None:
    sleeper = _spawn_sleeper()
    try:
        host = _StubHost(
            [{"id": "svc", "state": "running", "pid": sleeper.pid}], root_pid=os.getppid()
        )
        check = TreeSelfCheck(host)
        check.run_once()
        assert _chain(check)["broken"] is True
        events = recorder.events("root_chain_broken")
        assert cast("dict[str, object]", events[0]["reasons"]) == {"svc": "detached"}
    finally:
        sleeper.kill()
        sleeper.wait()


def test_unverifiable_is_separated_from_broken(
    monkeypatch: pytest.MonkeyPatch, recorder: _Recorder, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG, logger="services.supervision.meiva_root.selfcheck")

    def fake_child_state(pid: int, parent_pid: int) -> str:
        return "unverifiable"

    monkeypatch.setattr(selfcheck_mod, "child_state", fake_child_state)
    host = _StubHost([{"id": "svc", "state": "running", "pid": 12345}])
    check = TreeSelfCheck(host)

    check.run_once()
    chain = _chain(check)
    assert chain["broken"] is False
    assert chain["unverifiable_units"] == ["svc"]
    assert chain["unverifiable_for_s"] is not None
    assert recorder.events("root_chain_broken") == []
    warnings = [r for r in caplog.records if "cannot verify" in str(r.message)]
    assert len(warnings) == 1

    check.run_once()
    warnings = [r for r in caplog.records if "cannot verify" in str(r.message)]
    assert len(warnings) == 1

    host.units = []
    check.run_once()
    assert _chain(check)["unverifiable_units"] == []
    assert "verifiable again" in caplog.text


def test_config_validation() -> None:
    with pytest.raises(ValueError, match="interval_s"):
        SelfCheckConfig(interval_s=0)
    assert SelfCheckConfig().interval_s == 60.0


async def test_loop_start_stop() -> None:
    async with asyncio.TaskGroup() as tasks:
        check = TreeSelfCheck(_StubHost([]), config=SelfCheckConfig(interval_s=0.02), tasks=tasks)
        await check.start()
        with pytest.raises(RuntimeError, match="already started"):
            await check.start()
        await asyncio.sleep(0.06)
        await check.stop()
        assert cast(int, _chain(check)["rounds"]) >= 1
        await check.stop()


_SLEEP_FOREVER = [sys.executable, "-c", "import time; time.sleep(60)"]

StartFactory = Callable[..., Awaitable[Supervisor]]


def _unit(unit_id: str) -> UnitManifest:
    return UnitManifest(
        id=unit_id,
        exec=tuple(_SLEEP_FOREVER),
        restart=RestartPolicy.ALWAYS,
        attach="root",
    )


@pytest.fixture
async def started(short_tmp: Path) -> AsyncIterator[StartFactory]:
    created: list[Supervisor] = []

    async def factory(units: list[UnitManifest]) -> Supervisor:
        supervisor = Supervisor(UnitRegistry(units), run_dir=short_tmp)
        created.append(supervisor)
        await supervisor.start()
        return supervisor

    yield factory
    for supervisor in created:
        if supervisor._running:
            await supervisor.shutdown()


async def test_integration_detects_a_disarmed_death_and_allows_replacement(
    started: StartFactory, recorder: _Recorder
) -> None:
    supervisor = await started([_unit("svc")])
    units = cast("list[dict[str, object]]", supervisor.tree_view()["units"])
    pid = cast(int, units[0]["pid"])

    check = TreeSelfCheck(supervisor)
    check.run_once()
    assert _chain(check)["broken"] is False

    runtime = supervisor._units["svc"]
    assert runtime.watch_task is not None
    runtime.watch_task.cancel()
    os.kill(pid, signal.SIGKILL)
    await asyncio.sleep(0.15)

    check.run_once()
    chain = _chain(check)
    assert chain["broken"] is True
    assert chain["broken_units"] == ["svc"]
    assert len(recorder.events("root_chain_broken")) == 1

    result = cast("list[dict[str, object]]", (await supervisor.up("svc"))["units"])
    assert result[0]["action"] == "started"
    new_pid = cast(int, result[0]["pid"])
    assert new_pid != pid
