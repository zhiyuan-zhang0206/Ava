"""A real pty-sessions service under the test's home: the `pty_service` fixture.

Registered by the packages whose tests run real shells; tests elsewhere that
need one import the names from here. The service is a subprocess exactly as
root launches it (`python -m services.pty_sessions.daemon`), pinned to the
test's `AVA_HOME`, so a session survives its creating *test process's clients*
the way it survives an agent host, and a service crash is a real SIGKILL. The
service's `HOME` is the test home too: its login shells read no operator
profile.
"""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from base.sessions.pty import client
from base.sessions.pty.paths import service_socket_path

_REPO = Path(__file__).resolve().parents[2]

_READY_TIMEOUT_S = 20.0
_STOP_TIMEOUT_S = 30.0


class PtyServiceProcess:
    """The service subprocess of one test home; `start` it again after a crash."""

    def __init__(self, home: Path, env: dict[str, str] | None = None) -> None:
        self.home = home
        self.extra_env = env or {}
        self.log = home / "pty-sessions-service.log"
        self.process: subprocess.Popen[bytes] | None = None

    @property
    def pid(self) -> int:
        assert self.process is not None, "the service was never started"
        return self.process.pid

    def start(self) -> None:
        assert self.process is None or self.process.poll() is not None, "already running"
        env = {**os.environ, "AVA_HOME": str(self.home), "HOME": str(self.home), **self.extra_env}
        with self.log.open("ab") as log:
            self.process = subprocess.Popen(
                [sys.executable, "-m", "services.pty_sessions.daemon"],
                cwd=_REPO,
                env=env,
                stdout=log,
                stderr=log,
            )
        deadline = time.monotonic() + _READY_TIMEOUT_S
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                raise AssertionError(f"pty-sessions exited at startup:\n{self.output()}")
            with contextlib.suppress(client.ServiceUnavailableError):
                client.request("ping")
                return
            time.sleep(0.02)
        raise AssertionError(f"pty-sessions never answered a ping:\n{self.output()}")

    def output(self) -> str:
        return self.log.read_text(errors="replace") if self.log.exists() else ""

    def signal(self, sig: int) -> None:
        assert self.process is not None
        self.process.send_signal(sig)

    def wait(self, timeout: float = _STOP_TIMEOUT_S) -> int:
        assert self.process is not None
        return self.process.wait(timeout=timeout)

    def stop(self) -> None:
        """SIGTERM the service (it closes its sessions first); SIGKILL if it hangs."""
        proc = self.process
        if proc is None or proc.poll() is not None:
            return
        proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(timeout=_STOP_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=10)


@pytest.fixture
def pty_service(unit_home: Path) -> Iterator[PtyServiceProcess]:
    """A running pty-sessions service in this test's home; its sessions die with the test."""
    service = PtyServiceProcess(unit_home)
    service.start()
    yield service
    with contextlib.suppress(client.ServiceUnavailableError, client.ServiceError):
        client.close_all(grace_s=0.5, kill_s=2.0)
    service.stop()
    service_socket_path().unlink(missing_ok=True)
