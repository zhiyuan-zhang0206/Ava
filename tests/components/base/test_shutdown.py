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
from services.supervision.ava_root.manifest import RestartPolicy, UnitManifest, UnitRegistry
from services.supervision.ava_root.supervisor import Supervisor, SupervisorConfig
from tests.components.base.poll_until import poll_until


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


def test_shutdown_line_logs_a_registered_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The stop line carries the daemon name as `service=` and derives a
    registered event."""
    from base.events import EVENTS

    calls: list[dict[str, object]] = []

    class _Capture:
        def info(self, message: str, **kwargs: object) -> None:
            calls.append({"message": message, **kwargs})

    previous = signal.getsignal(signal.SIGTERM)
    try:
        monkeypatch.setattr(shutdown, "logger", _Capture())
        shutdown.install_graceful_shutdown("pg-backup")
        with pytest.raises(KeyboardInterrupt):
            signal.raise_signal(signal.SIGTERM)
    finally:
        signal.signal(signal.SIGTERM, previous)
    assert len(calls) == 1
    call = calls[0]
    assert call["message"] == "[{service}] received {sig}, shutting down"
    assert call["service"] == "pg-backup"
    assert call["sig"] == "SIGTERM"
    assert "label" not in call
    assert (call.get("event") or "log") in EVENTS


_SERVICE = """
import asyncio
import sys
import threading
from pathlib import Path
sys.path.insert(0, {repo!r})
from base.daemon.shutdown import install_graceful_shutdown
install_graceful_shutdown("private-service-test")

async def run():
    try:
        if {ops!r}:
            entered = threading.Event()
            def stuck_worker():
                entered.set()
                threading.Event().wait()
            asyncio.create_task(asyncio.to_thread(stuck_worker))
            while not entered.is_set():
                await asyncio.sleep(0.01)
        Path({started!r}).write_text("ready")
        await asyncio.Event().wait()
    finally:
        await asyncio.sleep(0.05)
        Path({marker!r}).write_text("clean")

if {ops!r}:
    from services.agent_runner.agent_ops import daemon
    async def ops_run(*, database, image, local_machine, config):
        await run()
    daemon.init_gateway_process = lambda **kwargs: None
    daemon.serve_ops_daemon = ops_run
    daemon.main()
else:
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass
"""


@pytest.mark.parametrize("ops", [False, True], ids=["asyncio-run", "ops-runner"])
async def test_root_graceful_stop_runs_the_product_handlers_cleanup(
    unit_home: Path, ops: bool
) -> None:
    """ava-root owns service stop: its graceful `down` (what a normal stop
    requests) must reach the product handler, whose cleanup then runs."""
    repo = Path(__file__).resolve().parents[3]
    script, started, marker = (unit_home / name for name in ("daemon.py", "started", "clean"))
    script.write_text(
        _SERVICE.format(repo=str(repo), started=str(started), marker=str(marker), ops=ops)
    )
    env = (("AVA_HOME", str(unit_home)), ("AVA_CONFIG_FETCH", "skip"))
    unit = UnitManifest("svc", (sys.executable, str(script)), RestartPolicy.NEVER, "root", env)
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
