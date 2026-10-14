"""Common signal registration and real supervised-daemon cleanup."""

import asyncio
import os
import signal
import subprocess
import sys
from pathlib import Path

import pytest

from base.daemon import shutdown
from base.host import exit_codes as hard_exit_module
from base.log.tests.log_records import LogRecords
from base.sessions.tests.poll_until import poll_until
from services.supervision.ava_root.manifest import RestartPolicy, UnitManifest, UnitRegistry
from services.supervision.ava_root.supervisor import Supervisor, SupervisorConfig


def test_cancel_and_drain_runs_cleanup_and_returns_only_ordinary_failures() -> None:
    cleaned: list[str] = []
    tasks: list[asyncio.Task[None]] = []

    async def cancelled() -> None:
        try:
            await asyncio.Event().wait()
        finally:
            cleaned.append("cancelled")

    async def failed() -> None:
        try:
            await asyncio.Event().wait()
        finally:
            cleaned.append("failed")
            raise RuntimeError("cleanup failed")

    async def start() -> None:
        tasks.append(asyncio.create_task(cancelled()))
        tasks.append(asyncio.create_task(failed()))
        await asyncio.sleep(0)

    runner = asyncio.Runner()
    try:
        runner.run(start())
        failures = shutdown.cancel_and_drain(runner)
        assert sorted(cleaned) == ["cancelled", "failed"]
        assert len(failures) == 1
        assert isinstance(failures[0], RuntimeError)
        assert str(failures[0]) == "cleanup failed"
    finally:
        runner.close()


def test_hard_exit_flushes_sinks_in_order_before_exiting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from loguru import logger as loguru_logger

    calls: list[str] = []

    class _Stream:
        def __init__(self, name: str) -> None:
            self.name = name

        def flush(self) -> None:
            calls.append(self.name)

    def exit_process(code: int) -> None:
        calls.append(f"exit:{code}")
        raise SystemExit(code)

    monkeypatch.setattr(loguru_logger, "remove", lambda: calls.append("loguru"))
    monkeypatch.setattr(hard_exit_module.logging, "shutdown", lambda: calls.append("logging"))
    monkeypatch.setattr(hard_exit_module.sys, "stdout", _Stream("stdout"))
    monkeypatch.setattr(hard_exit_module.sys, "stderr", _Stream("stderr"))
    monkeypatch.setattr(hard_exit_module.os, "_exit", exit_process)
    with pytest.raises(SystemExit) as stopped:
        hard_exit_module.hard_exit(7)
    assert stopped.value.code == 7
    assert calls == ["loguru", "logging", "stdout", "stderr", "exit:7"]


def test_hard_exit_reports_a_failed_step_on_stderr_and_still_exits(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from loguru import logger as loguru_logger

    def broken_remove() -> None:
        raise RuntimeError("sink close failed")

    def exit_process(code: int) -> None:
        raise SystemExit(code)

    monkeypatch.setattr(loguru_logger, "remove", broken_remove)
    monkeypatch.setattr(hard_exit_module.os, "_exit", exit_process)
    with pytest.raises(SystemExit) as stopped:
        hard_exit_module.hard_exit(3)
    assert stopped.value.code == 3
    err = capsys.readouterr().err
    assert "closing the loguru sinks failed" in err
    assert "sink close failed" in err


def test_shutdown_line_logs_a_registered_event(loguru_records: LogRecords) -> None:
    """The stop line carries the daemon name as `service=` and derives a
    registered event."""
    from base.events import EVENTS

    previous = signal.getsignal(signal.SIGTERM)
    try:
        shutdown.install_graceful_shutdown("pg-backup")
        with pytest.raises(KeyboardInterrupt):
            signal.raise_signal(signal.SIGTERM)
    finally:
        signal.signal(signal.SIGTERM, previous)
    assert loguru_records.messages() == ["[pg-backup] received SIGTERM, shutting down"]
    extra = loguru_records[0]["extra"]
    assert extra["service"] == "pg-backup"
    assert extra["sig"] == "SIGTERM"
    assert "label" not in extra
    assert (extra.get("event") or "log") in EVENTS


# The child imports the ops daemon, so it lives with that daemon's tests.
_CHILD = (
    Path(__file__).parent.parent.parent.parent
    / "services/agent_runner/agent_ops/tests/child_graceful_stop.py"
)


@pytest.mark.parametrize("mode", ["asyncio-run", "ops-runner"])
async def test_root_graceful_stop_runs_the_product_handlers_cleanup(
    unit_home: Path, mode: str
) -> None:
    """ava-root owns service stop: its graceful `down` (what a normal stop
    requests) must reach the product handler, whose cleanup then runs."""
    started, marker = (unit_home / name for name in ("started", "clean"))
    env = (("AVA_HOME", str(unit_home)), ("AVA_CONFIG_FETCH", "skip"))
    command = (sys.executable, str(_CHILD), str(started), str(marker), mode)
    unit = UnitManifest("svc", command, RestartPolicy.NEVER, "root", env)
    owner = Supervisor(
        UnitRegistry([unit]), run_dir=unit_home / "root", config=SupervisorConfig(stop_timeout_s=10)
    )
    await owner.start()
    try:
        await asyncio.to_thread(
            poll_until, started.exists, timeout=20, what="product signal handler is ready"
        )
        await owner.down("svc")
        assert marker.read_text() == "clean"
    finally:
        await owner.shutdown()


def test_plugin_restart_runs_the_product_handlers_cleanup(unit_home: Path) -> None:
    repo = Path(__file__).resolve().parents[3]
    marker = unit_home / "plugin-clean"
    env = dict(os.environ)
    env.update(AVA_HOME=str(unit_home), AVA_CONFIG_FETCH="skip")
    result = subprocess.run(  # noqa: S603 — fixed test-owned Python script
        [sys.executable, str(Path(__file__).with_name("plugin_restart_probe.py")), str(marker)],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert marker.read_text() == "clean"
