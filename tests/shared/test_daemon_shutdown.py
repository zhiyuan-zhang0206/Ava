"""Common signal registration and real supervised-daemon cleanup."""

import os
import shlex
import signal
import subprocess
import sys
from pathlib import Path

import pytest

from cli.commands import _maintenance_stop
from shared import daemon_shutdown
from shared.platform import IS_WINDOWS
from shared.session_backend import PosixProcSessionBackend, WinprocSessionBackend
from tests.shared.poll_until import poll_until


def test_windows_break_uses_the_shared_interrupt_handler(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A real spare POSIX signal exercises Windows registration on non-Windows
    # CI; the native-console test below covers actual Ctrl-Break delivery.
    if sys.platform == "win32":
        break_signal = signal.SIGBREAK
    else:
        break_signal = signal.SIGUSR1
    monkeypatch.setattr(signal, "SIGBREAK", break_signal, raising=False)
    previous = {sig: signal.getsignal(sig) for sig in (signal.SIGTERM, break_signal)}
    sigint = signal.getsignal(signal.SIGINT)
    try:
        with monkeypatch.context() as windows_registration:
            windows_registration.setattr(sys, "platform", "win32")
            daemon_shutdown.install_graceful_shutdown("signal-test")
        assert signal.getsignal(signal.SIGINT) is sigint
        assert signal.getsignal(break_signal) is signal.getsignal(signal.SIGTERM)
        for sig in previous:
            with pytest.raises(KeyboardInterrupt):
                signal.raise_signal(sig)
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)


_SERVICE = """
import asyncio
import sys
import threading
from pathlib import Path
sys.path.insert(0, {repo!r})
from shared.daemon_shutdown import install_graceful_shutdown
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
    from services.agent_ops import daemon
    daemon.init_gateway_process = lambda **kwargs: None
    daemon._main = run
    daemon.main()
else:
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass
"""


@pytest.mark.parametrize("ops", [False, True], ids=["asyncio-run", "ops-runner"])
def test_native_service_stop_runs_the_product_handlers_cleanup(
    unit_home: Path, monkeypatch: pytest.MonkeyPatch, ops: bool
) -> None:
    repo = Path(__file__).resolve().parents[2]
    script, started, marker = (unit_home / name for name in ("daemon.py", "started", "clean"))
    script.write_text(
        _SERVICE.format(repo=str(repo), started=str(started), marker=str(marker), ops=ops)
    )
    env = dict(os.environ)
    env.update(AVA_HOME=str(unit_home), AVA_HOME_OVERRIDE="1", AVA_CONFIG_FETCH="skip")
    backend = WinprocSessionBackend() if IS_WINDOWS else PosixProcSessionBackend()
    monkeypatch.setattr(_maintenance_stop, "get_backend", lambda: backend)
    argv = [sys.executable, str(script)]
    command = subprocess.list2cmdline(argv) if IS_WINDOWS else shlex.join(argv)
    name = "ava-private-signal-proof"
    assert backend.new_session(name, command, repo, env=env)
    try:
        poll_until(started.exists, timeout=20, what="product signal handler is ready")
        assert _maintenance_stop.stop_services(
            10, keep_terminals=True, selected=frozenset({name})
        ) == [name]
        assert marker.read_text() == "clean"
        assert not backend.has_session(name)
    finally:
        # Only fixture cleanup may force this exact private session after a
        # failed assertion; successful normal stop already removed its record.
        if backend.has_session(name):
            backend.kill_session(name, graceful=False)


_PLUGIN_SERVICE = """
import asyncio
import sys
from pathlib import Path
sys.path.insert(0, {repo!r})
from shared.daemon_shutdown import install_graceful_shutdown
from services.agent_host import daemon
install_graceful_shutdown("private-plugin-restart-test")
fingerprints = iter(["before", "after"])
daemon._plugins_fingerprint = lambda: next(fingerprints)
daemon._PLUGINS_POLL_INTERVAL_S = 0.01

async def run():
    try:
        await daemon._watch_plugins_for_restart()
    finally:
        await asyncio.sleep(0.05)
        Path({marker!r}).write_text("clean")

try:
    asyncio.run(run())
except KeyboardInterrupt:
    pass
"""


def test_plugin_restart_runs_the_product_handlers_cleanup(unit_home: Path) -> None:
    repo = Path(__file__).resolve().parents[2]
    marker = unit_home / "plugin-clean"
    code = _PLUGIN_SERVICE.format(repo=str(repo), marker=str(marker))
    env = dict(os.environ)
    env.update(AVA_HOME=str(unit_home), AVA_HOME_OVERRIDE="1", AVA_CONFIG_FETCH="skip")
    result = subprocess.run(  # noqa: S603 — fixed test-owned Python script
        [sys.executable, "-c", code],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert marker.read_text() == "clean"
