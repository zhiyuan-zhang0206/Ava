"""The exec memory guard: when it acts, whom it kills, and what the agent reads."""

from __future__ import annotations

import asyncio
import contextlib
import os
import time
from pathlib import Path
from typing import Any

import psutil
import pytest
from loguru import logger

from agent.graph.exec._result import _ExecCrashed, _ExecDone
from agent.graph.exec._subprocess import _result_from_payload, _run_in_subprocess
from base.db import Database
from base.host.memory_pressure import PressureLevel
from base.host.proc import kill_process_tree
from base.native_process.exec_kill_notice import notice_path, read_notice
from services.agent_runner.agent_host.exec_memory_guard import ExecDomain, ExecMemoryGuard

_GIB = 1024**3


class _Source:
    """Pressure levels are consumed one per read; the last one repeats."""

    def __init__(self, *levels: PressureLevel) -> None:
        self.levels = list(levels)
        self.reads = 0

    def pressure(self) -> PressureLevel:
        self.reads += 1
        return self.levels.pop(0) if len(self.levels) > 1 else self.levels[0]

    def footprint(self, pid: int) -> int:
        return 0


def _domain(tmp_path: Path, pid: int, gib: float) -> ExecDomain:
    return ExecDomain(
        pid=pid,
        create_time=1.0,
        agent_id=7,
        result_path=tmp_path / f"res-{pid}.json",
        footprint=int(gib * _GIB),
    )


def _guard(source: _Source, domains: list[ExecDomain], killed: list[ExecDomain]) -> ExecMemoryGuard:
    def kill(domain: ExecDomain) -> None:
        killed.append(domain)
        domains.remove(domain)

    return ExecMemoryGuard(source, domains=lambda: list(domains), kill=kill)


@pytest.mark.parametrize("level", ["normal", "warning"])
def test_below_critical_never_touches_an_exec(tmp_path: Path, level: PressureLevel) -> None:
    killed: list[ExecDomain] = []
    domains = [_domain(tmp_path, 10, 9.0)]
    called: list[str] = []
    guard = ExecMemoryGuard(
        _Source(level),
        domains=lambda: called.append("listed") or domains,
        kill=killed.append,
    )
    assert guard.check_once() is None
    assert killed == []
    assert called == []


def test_critical_kills_only_the_largest_and_records_why(tmp_path: Path) -> None:
    small, large, middle = (
        _domain(tmp_path, 10, 0.5),
        _domain(tmp_path, 11, 6.2),
        _domain(tmp_path, 12, 2.0),
    )
    killed: list[ExecDomain] = []
    guard = _guard(_Source("critical"), [small, large, middle], killed)

    assert guard.check_once() == large
    assert killed == [large]
    notice = read_notice(large.result_path)
    assert notice is not None
    assert "killed by the host memory guard" in notice
    assert "critical memory pressure" in notice
    assert "6.2 GiB of 3 running" in notice
    assert "batches" in notice
    assert read_notice(small.result_path) is None
    assert read_notice(middle.result_path) is None


def test_equal_footprints_break_the_tie_by_pid(tmp_path: Path) -> None:
    killed: list[ExecDomain] = []
    first, second = _domain(tmp_path, 10, 1.0), _domain(tmp_path, 20, 1.0)
    _guard(_Source("critical"), [first, second], killed).check_once()
    assert killed == [second]


def test_pressure_that_clears_after_one_kill_stops_the_guard(tmp_path: Path) -> None:
    killed: list[ExecDomain] = []
    domains = [_domain(tmp_path, 10, 4.0), _domain(tmp_path, 11, 3.0)]
    guard = _guard(_Source("critical", "normal"), domains, killed)

    assert guard.check_once() is not None
    assert guard.check_once() is None
    assert [domain.pid for domain in killed] == [10]
    assert [domain.pid for domain in domains] == [11]


def test_pressure_that_stays_critical_takes_the_next_largest_one_at_a_time(
    tmp_path: Path,
) -> None:
    killed: list[ExecDomain] = []
    domains = [_domain(tmp_path, 10, 4.0), _domain(tmp_path, 11, 3.0), _domain(tmp_path, 12, 1.0)]
    guard = _guard(_Source("critical"), domains, killed)

    guard.check_once()
    assert [domain.pid for domain in killed] == [10]
    guard.check_once()
    assert [domain.pid for domain in killed] == [10, 11]


def test_critical_with_no_exec_running_does_nothing() -> None:
    assert _guard(_Source("critical"), [], []).check_once() is None


def test_kill_emits_the_registered_event_with_agent_footprint_and_pressure(
    tmp_path: Path,
) -> None:
    records: list[Any] = []
    sink = logger.add(lambda message: records.append(message.record))
    try:
        _guard(_Source("critical"), [_domain(tmp_path, 10, 2.0)], []).check_once()
    finally:
        logger.remove(sink)
    [record] = [row for row in records if row["extra"].get("event") == "exec_memory_guard_killed"]
    assert record["level"].name == "WARNING"
    extra = record["extra"]
    assert extra["agent_id"] == 7
    assert extra["footprint_bytes"] == 2 * _GIB
    assert extra["running"] == 1
    assert extra["pressure"] == "critical"


def test_the_notice_is_consumed_by_the_run_that_reads_it(tmp_path: Path) -> None:
    result = tmp_path / "res.json"
    notice_path(result).write_text('{"notice": "why"}', encoding="utf-8")
    assert read_notice(result) == "why"
    assert not notice_path(result).exists()
    assert read_notice(result) is None


def test_a_guard_killed_run_reports_the_reason_not_a_missing_envelope() -> None:
    result = _result_from_payload(
        "partial output\n",
        None,
        cancelled=False,
        timed_out=False,
        memory_guard_notice="[exec killed by the host memory guard: because]",
    )
    assert isinstance(result, _ExecCrashed)
    assert "partial output" in result.output
    assert "killed by the host memory guard" in result.output
    assert "without writing a result envelope" not in str(result.exc)
    assert "killed by the host memory guard" in str(result.exc)


def test_without_a_notice_a_missing_envelope_keeps_its_generic_error() -> None:
    result = _result_from_payload("", None, cancelled=False, timed_out=False)
    assert isinstance(result, _ExecCrashed)
    assert "without writing a result envelope" in str(result.exc)


class _CriticalOs:
    """The real OS process table with the pressure level pinned to critical."""

    def pressure(self) -> PressureLevel:
        return "critical"

    def footprint(self, pid: int) -> int:
        return psutil.Process(pid).memory_info().rss


async def _run(tmp_path: Path, code: str) -> Any:
    result, _payload = await _run_in_subprocess(
        Database.from_settings(),
        code,
        424242,
        asyncio.Event(),
        60.0,
        None,
        exec_dir=tmp_path / "exec",
    )
    return result


async def _gone(pid: int) -> bool:
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        try:
            if psutil.Process(pid).status() == psutil.STATUS_ZOMBIE:
                return True
        except psutil.NoSuchProcess:
            return True
        await asyncio.sleep(0.05)
    return False


async def test_a_real_exec_is_killed_with_its_reason_and_the_host_keeps_serving(
    tmp_path: Path,
) -> None:
    """At critical pressure the guard finds the real running exec domain by itself,
    kills it, and the owning run reports why; the descendant is reaped and the next
    exec runs normally."""
    source = _CriticalOs()
    pid_file = tmp_path / "guard.pid"
    code = (
        "import pathlib, subprocess, sys, time\n"
        "descendant = subprocess.Popen(\n"
        "    [sys.executable, '-c', 'import time; time.sleep(60)']\n"
        ")\n"
        f"pathlib.Path({str(pid_file)!r}).write_text(str(descendant.pid), encoding='utf-8')\n"
        "print('working', flush=True)\n"
        "time.sleep(60)\n"
    )
    from services.agent_runner.agent_host.exec_memory_guard import find_exec_domains

    guard = ExecMemoryGuard(source, domains=lambda: find_exec_domains(os.getpid(), source))
    descendant_pid: int | None = None
    run = asyncio.create_task(_run(tmp_path, code))
    try:
        deadline = time.monotonic() + 30.0
        while not pid_file.exists() and time.monotonic() < deadline:
            await asyncio.sleep(0.1)
        descendant_pid = int(pid_file.read_text(encoding="utf-8"))

        victim = await asyncio.to_thread(guard.check_once)
        assert victim is not None
        assert victim.agent_id == 424242

        result = await run
        assert isinstance(result, _ExecCrashed)
        assert "killed by the host memory guard" in result.output
        assert "working" in result.output
        assert "without writing a result envelope" not in str(result.exc)
        assert await _gone(descendant_pid)

        after = await _run(tmp_path, "print('still serving')")
        assert isinstance(after, _ExecDone)
    finally:
        run.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await run
        if descendant_pid is not None:
            kill_process_tree(descendant_pid, grace_s=0.0)
