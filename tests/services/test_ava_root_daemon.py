"""services.ava_root.daemon: the standalone process, end to end.

These tests run the daemon as a real child process (`python -m
services.ava_root`), talk to it over its unix socket with the real client, and
pin the process-level contract: one tree per run directory, a clean SIGTERM
stop, and — critically — an instance lock that dies with its owner and is
never inherited by the units.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from typing import cast

from services.ava_root.client import RootClient, RootClientError
from services.ava_root.ipc import ResponsePayload

REPO_ROOT = Path(__file__).resolve().parents[2]

_SLEEPER = [sys.executable, "-u", "-c", "import time; time.sleep(300)"]

_SOCKET_NAME = "ava-root.sock"


def _write_manifests(base: Path, units: list[dict[str, object]]) -> Path:
    path = base / "units.json"
    path.write_text(json.dumps({"units": units}), encoding="utf-8")
    return path


def _daemon_command(run_dir: Path, manifests: Path) -> list[str]:
    return [
        sys.executable,
        "-m",
        "services.ava_root",
        "--run-dir",
        str(run_dir),
        "--manifests",
        str(manifests),
    ]


@contextmanager
def _daemon(run_dir: Path, manifests: Path) -> Generator[tuple[subprocess.Popen[bytes], Path]]:
    """Run a daemon; guarantee it is gone (TERM, then KILL) at scope exit."""
    run_dir.mkdir(parents=True, exist_ok=True)
    log_path = run_dir / "daemon.log"
    with log_path.open("ab") as log_file:
        proc = subprocess.Popen(  # noqa: S603 — fixed argv, this module's own command, no shell
            _daemon_command(run_dir, manifests),
            cwd=REPO_ROOT,
            stdout=log_file,
            stderr=subprocess.STDOUT,
        )
    try:
        yield proc, log_path
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)


def _read_log(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def _wait_ready(run_dir: Path, proc: subprocess.Popen[bytes], log_path: Path) -> RootClient:
    """Poll the control socket until the daemon answers; fail loudly on early exit."""
    socket_path = run_dir / _SOCKET_NAME
    client = RootClient(socket_path, timeout=5.0)
    deadline = time.monotonic() + 15.0
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise AssertionError(
                f"daemon exited early with {proc.returncode}; log:\n{_read_log(log_path)}"
            )
        if socket_path.exists():
            try:
                response = client.status()
            except RootClientError:
                response = None
            if response is not None and response["ok"]:
                return client
        time.sleep(0.05)
    raise AssertionError(f"daemon did not become ready; log:\n{_read_log(log_path)}")


def _units_of(response: ResponsePayload) -> list[dict[str, object]]:
    result = cast("dict[str, object]", response.get("result"))
    return cast("list[dict[str, object]]", result["units"])


def _assert_alive(pid: int) -> None:
    os.kill(pid, 0)


def _wait_dead(pid: int, *, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.05)
    raise AssertionError(f"pid {pid} still present")


def _wait_orphaned(pid: int, *, timeout: float = 5.0) -> None:
    """Wait until the process is reparented to init (its parent died)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        out = subprocess.run(  # noqa: S603 — fixed system tool, literal argv, no shell
            ["ps", "-o", "ppid=", "-p", str(pid)],
            capture_output=True,
            text=True,
            check=True,
        )
        if out.stdout.strip() == "1":
            return
        time.sleep(0.05)
    raise AssertionError(f"pid {pid} was not reparented to init")


def _kill_quietly(pid: int) -> None:
    try:
        os.kill(pid, 9)
    except ProcessLookupError:
        return
    _wait_dead(pid)


def test_daemon_end_to_end(short_tmp: Path) -> None:
    run_dir = short_tmp / "nested" / "run"  # the daemon creates its run dir
    manifests = _write_manifests(short_tmp, [{"id": "svc", "exec": _SLEEPER, "restart": "always"}])
    with _daemon(run_dir, manifests) as (proc, log_path):
        client = _wait_ready(run_dir, proc, log_path)

        response = client.status()
        result = cast("dict[str, object]", response.get("result"))
        root = cast("dict[str, object]", result["root"])
        assert root["pid"] == proc.pid
        units = _units_of(response)
        assert [u["id"] for u in units] == ["svc"]
        assert units[0]["state"] == "running"
        unit_pid = cast(int, units[0]["pid"])
        assert (run_dir / "logs" / "svc.log").exists()

        response = client.down("svc")
        assert response["ok"] is True
        assert _units_of(response)[0]["action"] == "stopped"
        _wait_dead(unit_pid)

        response = client.up("svc")
        assert response["ok"] is True
        revived = _units_of(response)[0]
        assert revived["action"] == "started"
        revived_pid = cast(int, revived["pid"])
        assert revived_pid != unit_pid

        response = client.upgrade()
        assert response["ok"] is False
        assert response.get("code") == "not_implemented"

        response = client.restart("svc")
        assert response["ok"] is True

    # SIGTERM ran the graceful stop: process exited 0, tree is down, socket gone.
    assert proc.returncode == 0
    assert not (run_dir / _SOCKET_NAME).exists()
    assert (run_dir / "ava-root.lock").exists()


def test_second_daemon_refuses_the_same_run_dir(short_tmp: Path) -> None:
    run_dir = short_tmp / "run"
    manifests = _write_manifests(short_tmp, [{"id": "svc", "exec": _SLEEPER, "restart": "always"}])
    with _daemon(run_dir, manifests) as (proc, log_path):
        client = _wait_ready(run_dir, proc, log_path)
        second = subprocess.Popen(  # noqa: S603 — fixed argv, this module's own command
            _daemon_command(run_dir, manifests),
            cwd=REPO_ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        out, _ = second.communicate(timeout=15)
        assert second.returncode == 1
        assert "another root supervisor" in out.decode()
        # The first daemon is unaffected.
        assert client.status()["ok"] is True


def test_lock_dies_with_the_daemon_and_is_not_inherited_by_units(short_tmp: Path) -> None:
    """The crash-recovery premise: a killed supervisor releases the lock even
    while its units keep running, so a fresh supervisor can take the tree."""
    run_dir = short_tmp / "run"
    manifests = _write_manifests(short_tmp, [{"id": "svc", "exec": _SLEEPER, "restart": "always"}])
    with _daemon(run_dir, manifests) as (proc_a, log_a):
        client_a = _wait_ready(run_dir, proc_a, log_a)
        unit_pid = cast(int, _units_of(client_a.status())[0]["pid"])
        try:
            # Hard-crash the supervisor: no cleanup runs.
            proc_a.kill()
            proc_a.wait(timeout=10)
            _assert_alive(unit_pid)  # the unit outlives its parent
            _wait_orphaned(unit_pid)  # reparented to init — the chain root is gone

            # A fresh daemon takes the same run dir: the lock went with the dead
            # process, and no unit inherited it.
            with _daemon(run_dir, manifests) as (proc_b, log_b):
                client_b = _wait_ready(run_dir, proc_b, log_b)
                new_pid = cast(int, _units_of(client_b.status())[0]["pid"])
                assert new_pid != unit_pid
                # Cleanly stop the new tree; the old orphan is ours to reap.
                client_b.down("svc")
        finally:
            _kill_quietly(unit_pid)


def test_bad_manifests_exit_nonzero(short_tmp: Path) -> None:
    run_dir = short_tmp / "run"
    bad = short_tmp / "units.json"
    bad.write_text("{ not json", encoding="utf-8")
    result = subprocess.run(  # noqa: S603 — fixed argv, this module's own command
        _daemon_command(run_dir, bad),
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert result.returncode == 1
    assert "not valid JSON" in result.stderr
    assert not (run_dir / _SOCKET_NAME).exists()
