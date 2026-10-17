"""services.supervision.meiva_root.daemon: the standalone process, end to end.

These tests run the daemon as a real child process (`python -m
services.supervision.meiva_root`), talk to it over its unix socket with the real client, and
pin the process-level contract: one tree per run directory, a clean SIGTERM
stop, and — critically — an instance lock that dies with its owner and is
never inherited by the units.
"""

from __future__ import annotations

import json
import os
import re
import stat
import subprocess
import sys
import time
from collections.abc import Callable, Generator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import cast

import httpx
import psutil
import pytest

from base.host.exit_codes import HELPER_CHAIN_BROKEN_EXIT_CODE
from base.host.system.boot_unit import BootUnitContext
from base.host.system.hostport import Host
from base.native_process.ownership import OwnedProcess
from base.native_process.root_control.client import RootClient, RootClientError
from base.native_process.root_control.ipc import ResponsePayload

REPO_ROOT = Path(__file__).resolve().parents[4]

_SLEEPER = [sys.executable, "-u", "-c", "import time; time.sleep(300)"]

_SOCKET_NAME = "meiva-root.sock"


def _write_manifests(base: Path, units: list[dict[str, object]]) -> Path:
    path = base / "units.json"
    path.write_text(json.dumps({"units": units}), encoding="utf-8")
    return path


def _daemon_command(
    run_dir: Path,
    manifests: Path,
    *,
    wiring: str | None = None,
    extra: Sequence[str] = (),
) -> list[str]:
    command = [
        sys.executable,
        "-m",
        "services.supervision.meiva_root",
        "--run-dir",
        str(run_dir),
        "--manifests",
        str(manifests),
    ]
    if wiring is not None:
        command.extend(["--wiring", wiring])
    command.extend(extra)
    return command


@contextmanager
def _daemon(
    run_dir: Path,
    manifests: Path,
    *,
    wiring: str | None = None,
    env: dict[str, str] | None = None,
    extra: Sequence[str] = (),
) -> Generator[tuple[subprocess.Popen[bytes], Path]]:
    """Run a daemon; guarantee it is gone (TERM, then KILL) at scope exit."""
    run_dir.mkdir(parents=True, exist_ok=True)
    log_path = run_dir / "daemon.log"
    with log_path.open("ab") as log_file:
        proc = subprocess.Popen(  # noqa: S603 — fixed argv, this module's own command, no shell
            _daemon_command(run_dir, manifests, wiring=wiring, extra=extra),
            cwd=REPO_ROOT,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            env=env,
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


def _wait_for(predicate: Callable[[], bool], message: str) -> None:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError(message)


def _assert_first_status_and_unit_pid(
    client: RootClient, proc: subprocess.Popen[bytes], run_dir: Path
) -> int:
    """The freshly ready daemon reports itself and one running `svc`; returns that unit's pid."""
    # K3 face: the control socket is owner-only.
    assert stat.S_IMODE((run_dir / _SOCKET_NAME).stat().st_mode) == 0o600

    response = client.status()
    result = cast("dict[str, object]", response.get("result"))
    root = cast("dict[str, object]", result["root"])
    assert root["pid"] == proc.pid
    units = _units_of(response)
    assert [u["id"] for u in units] == ["svc"]
    assert units[0]["state"] == "running"
    assert (run_dir / "logs" / "svc" / "output.log").exists()
    return cast(int, units[0]["pid"])


def _assert_down_up_restart_cycle(client: RootClient, unit_pid: int) -> None:
    """`down` stops the unit, `up` starts a different process, `restart` succeeds."""
    response = client.down("svc")
    assert response["ok"] is True
    assert _units_of(response)[0]["action"] == "stopped"
    _wait_dead(unit_pid)

    response = client.up("svc")
    assert response["ok"] is True
    revived = _units_of(response)[0]
    assert revived["action"] == "started"
    assert cast(int, revived["pid"]) != unit_pid

    response = client.restart("svc")
    assert response["ok"] is True


def test_daemon_end_to_end(short_tmp: Path) -> None:
    run_dir = short_tmp / "nested" / "run"  # the daemon creates its run dir
    manifests = _write_manifests(short_tmp, [{"id": "svc", "exec": _SLEEPER, "restart": "always"}])
    with _daemon(run_dir, manifests) as (proc, log_path):
        client = _wait_ready(run_dir, proc, log_path)
        unit_pid = _assert_first_status_and_unit_pid(client, proc, run_dir)
        _assert_down_up_restart_cycle(client, unit_pid)

    # SIGTERM ran the graceful stop: process exited 0, tree is down, socket gone.
    assert proc.returncode == 0
    assert not (run_dir / _SOCKET_NAME).exists()
    assert (run_dir / "meiva-root.lock").exists()


_FAST_HELPER_CHAIN = ("--helper-chain-interval", "0.05", "--helper-chain-confirm-rounds", "1")


def test_retires_when_the_helper_ancestry_is_gone(short_tmp: Path) -> None:
    run_dir = short_tmp / "run"
    manifests = _write_manifests(short_tmp, [{"id": "svc", "exec": _SLEEPER, "restart": "never"}])
    env = {**os.environ, "MEIVA_PERMISSIONS_HELPER_PID": str(2**31 - 1)}
    with _daemon(run_dir, manifests, env=env, extra=_FAST_HELPER_CHAIN) as (proc, log):
        assert proc.wait(timeout=30) == HELPER_CHAIN_BROKEN_EXIT_CODE, _read_log(log)
    assert "helper ancestry is gone" in _read_log(log)
    assert not (run_dir / _SOCKET_NAME).exists()


def test_keeps_running_while_the_helper_ancestry_is_intact(short_tmp: Path) -> None:
    run_dir = short_tmp / "run"
    manifests = _write_manifests(short_tmp, [{"id": "svc", "exec": _SLEEPER, "restart": "never"}])
    env = {**os.environ, "MEIVA_PERMISSIONS_HELPER_PID": str(os.getpid())}
    with _daemon(run_dir, manifests, env=env, extra=_FAST_HELPER_CHAIN) as (proc, log):
        client = _wait_ready(run_dir, proc, log)
        time.sleep(0.5)
        assert proc.poll() is None, _read_log(log)
        assert client.status()["ok"] is True


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


def _stubborn(ready: Path) -> list[str]:
    """A unit that ignores TERM once it has written `ready`."""
    return [
        sys.executable,
        "-u",
        "-c",
        (
            "import signal,time,pathlib; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
            f"pathlib.Path({str(ready)!r}).write_text('ready'); time.sleep(300)"
        ),
    ]


@pytest.mark.parametrize("reaped_first", [False, True])
def test_termination_after_unit_exit_exits(short_tmp: Path, reaped_first: bool) -> None:
    """A unit that exited before any stop does not hold root past SIGTERM.

    systemd's root boot unit sets SendSIGKILL=no: TERM is the only stop, so a
    root must not wait for a second stop request. That holds too when
    the leader was reaped before root read its birth.
    """
    command = [sys.executable, "-c", "pass"]
    manifests = _write_manifests(short_tmp, [{"id": "svc", "exec": command, "restart": "never"}])
    run_dir = short_tmp / "run"
    with _daemon(
        run_dir,
        manifests,
        wiring=_wiring("wiring_reaped_first") if reaped_first else None,
        env=_wiring_env(short_tmp / "markers") if reaped_first else None,
    ) as (root, log):
        client = _wait_ready(run_dir, root, log)
        _wait_for(
            lambda: _units_of(client.status())[0]["pid"] is None, "unit did not exit on its own"
        )
        if reaped_first:
            assert _units_of(client.status())[0]["create_time"] is None, "root read its birth"
        assert not (run_dir / "custody").exists()
        root.terminate()
        assert root.wait(timeout=10) == 0, _read_log(log)


def test_lock_dies_with_the_daemon_and_is_not_inherited_by_units(short_tmp: Path) -> None:
    """A crashed owner releases its singleton lock, without a durable admission record."""
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

            with _daemon(run_dir, manifests) as (proc_b, log_b):
                client_b = _wait_ready(run_dir, proc_b, log_b)
                replacement = cast(int, _units_of(client_b.status())[0]["pid"])
                assert replacement != unit_pid
                _assert_alive(replacement)
                assert client_b.shutdown()["ok"]
                assert proc_b.wait(timeout=10) == 0
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


def _wiring(module: str) -> str:
    """The `--wiring` reference of a hook module that lives beside this test."""
    return f"services.supervision.meiva_root.tests.hooks.{module}:build"


def _wiring_env(markers: Path, *, fault: Path | None = None) -> dict[str, str]:
    env = dict(os.environ)
    env["MEIVA_ROOT_WIRING_MARKERS"] = str(markers)
    if fault is not None:
        env["MEIVA_ROOT_WIRING_FAULT"] = str(fault)
    return env


def test_wiring_hook_start_stop_lifecycle(short_tmp: Path) -> None:
    run_dir = short_tmp / "run"
    manifests = _write_manifests(short_tmp, [{"id": "svc", "exec": _SLEEPER, "restart": "always"}])
    markers = short_tmp / "markers"
    with _daemon(run_dir, manifests, wiring=_wiring("wiring_record"), env=_wiring_env(markers)) as (
        proc,
        log_path,
    ):
        client = _wait_ready(run_dir, proc, log_path)
        assert (markers / "started").exists()
        assert "wired participant(s)" in _read_log(log_path)
        assert client.status()["ok"] is True
    assert proc.returncode == 0
    assert (markers / "stopped").exists()


def test_wiring_failure_refuses_startup(short_tmp: Path) -> None:
    run_dir = short_tmp / "run"
    manifests = _write_manifests(short_tmp, [{"id": "svc", "exec": _SLEEPER, "restart": "always"}])
    broken = subprocess.run(  # noqa: S603 — fixed argv, this module's own command
        _daemon_command(run_dir, manifests, wiring="no_such_module_xyz:build"),
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert broken.returncode == 1
    assert "cannot import wiring module" in broken.stderr
    assert not (run_dir / _SOCKET_NAME).exists()

    malformed = subprocess.run(  # noqa: S603 — fixed argv, this module's own command
        _daemon_command(run_dir, manifests, wiring="no_colon"),
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert malformed.returncode == 1
    assert "module:attribute" in malformed.stderr


def test_per_unit_log_directories(short_tmp: Path) -> None:
    run_dir = short_tmp / "run"
    manifests = _write_manifests(
        short_tmp,
        [
            {"id": "unit-a", "exec": _SLEEPER, "restart": "always"},
            {"id": "unit-b", "exec": _SLEEPER, "restart": "always"},
        ],
    )
    with _daemon(run_dir, manifests) as (proc, log_path):
        _wait_ready(run_dir, proc, log_path)
        assert (run_dir / "logs" / "unit-a" / "output.log").exists()
        assert (run_dir / "logs" / "unit-b" / "output.log").exists()
        assert not (run_dir / "logs" / "unit-a.log").exists()


def _root_pid(result: dict[str, object]) -> int:
    return cast(int, cast("dict[str, object]", result["root"])["pid"])


def _root_uptime(result: dict[str, object]) -> float:
    return cast(float, cast("dict[str, object]", result["root"])["uptime_s"])


def _systemd_starter(
    home: Path, receipt: Path, run_dir: Path, manifest: Path, failure: str
) -> tuple[Path, Path]:
    """A disposable readiness caller for the real generic root, not a deployed wrapper.

    Returns the starter script and the parameter file it reads.
    """
    parameters = receipt.parent / "starter.json"
    parameters.write_text(
        json.dumps(
            {
                "receipt": str(receipt),
                "home": str(home),
                "data_command": _SLEEPER,
                "root_command": _daemon_command(run_dir, manifest),
                "socket": str(run_dir / _SOCKET_NAME),
                "failure": failure,
            }
        )
    )
    return Path(__file__).parent / "hooks" / "systemd_starter.py", parameters


def _terminate_systemd_test_births(receipt: Path) -> None:
    from base.native_process.ownership import OwnedProcess

    if not receipt.exists():
        return
    births = json.loads(receipt.read_text())
    # Let root close its application before cleaning any independent leftovers.
    # Signalling both at once would invalidate root's stop capture artificially.
    for name in ("root", "app", "data"):
        if name not in births:
            continue
        owner = OwnedProcess(**births[name])
        if owner.live():
            process = psutil.Process(owner.pid)
            if OwnedProcess.capture(process) == owner:
                process.terminate()
        deadline = time.monotonic() + 15
        while owner.live() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert not owner.live(), f"test-owned {name} did not close"


def _systemd_test_context(home: Path) -> BootUnitContext:
    import grp
    import pwd

    entry = pwd.getpwuid(os.getuid())
    return BootUnitContext(
        home,
        REPO_ROOT,
        entry.pw_name,
        grp.getgrgid(entry.pw_gid).gr_name,
        Path(entry.pw_dir),
    )


def _assert_failed_adoption(ctx: BootUnitContext, receipt: Path, failure: str) -> None:
    from base.host.system import boot_unit

    assert receipt.with_suffix(".failure").read_text() == failure
    births = json.loads(receipt.read_text())
    assert set(births) == {"data", "root", "app"}
    assert boot_unit.manager_properties(host=Host.system())["MainPID"] == "0"
    # Before publication the deliberately stale hint names data; afterwards it
    # names root. Neither becomes MainPID when the ordinary starter fails.
    expected = births["data" if failure == "before" else "root"]["pid"]
    hint = boot_unit.root_pid_path(ctx.home)
    if hint.exists():  # Native manager may remove the failed unit's hint.
        assert hint.read_text().strip() == str(expected)


def _require_native_systemd() -> None:
    from base.host.system import boot_unit

    if sys.platform != "linux" or not boot_unit.systemd_running(host=Host.system()):
        pytest.skip("native Linux systemd required")
    allowed = subprocess.run(["sudo", "-n", "true"], check=False, timeout=10)
    if allowed.returncode:
        pytest.skip("native systemd test requires passwordless sudo")
    # The host has one boot unit and the test must be that unit to be adopted, so it
    # runs only where none exists: otherwise it would start, stop and reset the
    # host's own.
    known = subprocess.run(  # noqa: S603 — fixed systemctl verb, the module's own unit name
        ["systemctl", "show", "--property=LoadState", "--value", boot_unit.UNIT_NAME],
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    if known.stdout.strip() != "not-found":
        pytest.skip("this host has its own meiva-boot unit; the test would act on it")


@pytest.fixture
def native_systemd_unit(tmp_path_factory: pytest.TempPathFactory) -> Generator[None]:
    """Serialize the fixed unit's safety check, lifecycle and cleanup across workers."""
    if sys.platform != "linux":
        pytest.skip("native Linux systemd required")

    import fcntl

    # xdist worker basetemps are siblings under the same test-run directory.
    lock_path = tmp_path_factory.getbasetemp().parent / "meiva-boot-test.lock"
    with lock_path.open("a") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        _require_native_systemd()
        yield


@pytest.mark.parametrize("failure", ["", "before", "after"])
@pytest.mark.usefixtures("native_systemd_unit")
def test_native_systemd_root_lifetime(tmp_path: Path, failure: str) -> None:
    """Actual manager handoff, root TERM closure, and data sibling retention.

    The retained sleeper represents a separately owned native data process;
    this does not claim database protocol or durability verification. The
    dedicated Ubuntu CI step asserts systemd and sudo before invoking this test.
    """
    from base.host.system import boot_unit

    ctx = _systemd_test_context(tmp_path / "home")
    ctx.home.mkdir()
    run_dir = ctx.home / "root"
    manifest = _write_manifests(tmp_path, [{"id": "app", "exec": _SLEEPER, "restart": "always"}])
    receipt = tmp_path / "births.json"
    starter, parameters = _systemd_starter(ctx.home, receipt, run_dir, manifest, failure)
    unit = boot_unit.UNIT_NAME
    target = Path("/run/systemd/system") / unit
    holder = tmp_path / unit
    content = boot_unit.render_unit(ctx)
    exec_line = next(row for row in content.splitlines() if row.startswith("ExecStart="))
    replacement = (
        f"ExecStart=:{boot_unit.quote(sys.executable, 'python')}"
        f" {boot_unit.quote(str(starter), 'test starter')}"
        f" {boot_unit.quote(str(parameters), 'starter parameters')}"
    )
    holder.write_text(
        content.replace(exec_line, replacement).replace("Restart=on-failure", "Restart=no")
    )

    def native(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(  # noqa: S603 — fixed test-owned unit and explicit native verbs
            ["sudo", "-n", *args], capture_output=True, text=True, check=False, timeout=45
        )

    def require(*args: str) -> None:
        result = native(*args)
        assert result.returncode == 0, result.stdout + result.stderr

    try:
        require("install", "-m", "0644", str(holder), str(target))
        require("systemctl", "daemon-reload")
        started = native("systemctl", "start", unit)
        if failure:
            assert started.returncode != 0
            _assert_failed_adoption(ctx, receipt, failure)
            return
        assert started.returncode == 0, started.stdout + started.stderr
        raw = json.loads(receipt.read_text())
        # Receipt keys use OwnedProcess's dataclass spelling; status uses create_time.
        from base.native_process.ownership import OwnedProcess

        owners = {key: OwnedProcess(**value) for key, value in raw.items()}
        root, app, data = owners["root"], owners["app"], owners["data"]
        manager = boot_unit.manager_properties(host=Host.system())
        assert manager["MainPID"] == str(root.pid)
        assert manager["ActiveState"] == "active"
        assert psutil.Process(root.pid).ppid() == 1, "manager did not adopt root as its child"
        assert all(owner.live() for owner in owners.values())
        require("systemctl", "stop", unit)
        assert not root.live() and not app.live()
        assert data.live(), "application-root stop killed independent data-plane sibling"
        assert not list((run_dir / "custody").glob("*.json"))
    finally:
        native("systemctl", "stop", unit)
        _terminate_systemd_test_births(receipt)
        native("rm", "-f", str(target))
        native("systemctl", "daemon-reload")
        native("systemctl", "reset-failed", unit)


def test_failed_shutdown_reports_error_and_exits_without_another_stop(short_tmp: Path) -> None:
    """A live child's refusal is concrete, without a persistent custody owner loop."""
    ready = short_tmp / "child-ready"
    manifests = _write_manifests(
        short_tmp, [{"id": "svc", "exec": _stubborn(ready), "restart": "never"}]
    )
    run_dir = short_tmp / "run"
    with _daemon(
        run_dir,
        manifests,
        wiring=_wiring("wiring_short_deadline"),
        env=_wiring_env(short_tmp / "markers"),
    ) as (proc, log):
        client = _wait_ready(run_dir, proc, log)
        _wait_for(ready.exists, "child did not install its TERM handler")
        child = cast(int, _units_of(client.status())[0]["pid"])
        try:
            assert client.shutdown()["ok"]
            assert proc.wait(timeout=5) != 0
            assert "unit svc did not stop" in _read_log(log)
            _assert_alive(child)
            assert not (run_dir / "custody").exists()
        finally:
            _kill_quietly(child)


@pytest.mark.parametrize("refuses_stop", [False, True])
def test_unknown_monitor_failure_exits_and_preserves_cleanup_refusal(
    short_tmp: Path, refuses_stop: bool
) -> None:
    ready = short_tmp / "child-ready"
    fault = short_tmp / "fail-round"
    units: list[dict[str, object]] = (
        [{"id": "svc", "exec": _stubborn(ready), "restart": "never"}] if refuses_stop else []
    )
    manifests = _write_manifests(short_tmp, units)
    run_dir = short_tmp / "run"
    with _daemon(
        run_dir,
        manifests,
        wiring=_wiring("wiring_unknown_monitor"),
        env=_wiring_env(short_tmp / "markers", fault=fault),
    ) as (proc, log):
        client = _wait_ready(run_dir, proc, log)
        child = None
        if refuses_stop:
            _wait_for(ready.exists, "child did not install its TERM handler")
            child = cast(int, _units_of(client.status())[0]["pid"])
        try:
            fault.touch()
            assert proc.wait(timeout=5) != 0
            detail = _read_log(log)
            assert "unknown-monitor-primary" in detail
            if child is not None:
                assert "unit svc did not stop" in detail
                _assert_alive(child)
            assert not (run_dir / "custody").exists()
        finally:
            if child is not None:
                _kill_quietly(child)


def _serving_port(run_dir: Path, unit_id: str) -> int | None:
    log = run_dir / "logs" / unit_id / "output.log"
    match = re.search(r"Serving HTTP on .* port (\d+)", log.read_text()) if log.exists() else None
    return int(match[1]) if match else None


def _assert_serving_siblings(run_dir: Path) -> None:
    for unit_id in ("gateway", "frontend"):
        port = _serving_port(run_dir, unit_id)
        assert port is not None
        response = httpx.get(f"http://127.0.0.1:{port}/reply", timeout=1, trust_env=False)
        assert response.status_code == 200 and response.text == "still serving"


def _status_births(result: dict[str, object]) -> dict[str, tuple[object, ...]]:
    root = cast("dict[str, object]", result["root"])
    units = cast("list[dict[str, object]]", result["units"])
    return {
        name: (entry["pid"], entry["create_time"], entry["starttime"])
        for name, entry in [("root", root), *((str(unit["id"]), unit) for unit in units)]
    }


def _assert_refused_generation(current: dict[str, object]) -> None:
    failed = next(
        unit
        for unit in cast("list[dict[str, object]]", current["units"])
        if unit["id"] == "agent-host"
    )
    assert failed["intent"] == "running" and failed["restart_count"] == 0
    assert cast("dict[str, object]", failed["restart_failed"])["stage"] == "down"
    retained = psutil.Process(cast(int, failed["pid"]))
    assert retained.create_time() == failed["create_time"]
    assert retained.status() != psutil.STATUS_ZOMBIE, "automatic recovery killed the child"


def _assert_budget_and_observation(run_dir: Path, current: dict[str, object]) -> None:
    health = cast("dict[str, dict[str, object]]", current["health"])
    assert health["agent-host"]["restart_attempts"] == 3
    assert health["agent-host"]["restart_limit_reached"] is True
    assert health["agent-host"]["last_verdict"] == "down"
    for unit_id in ("gateway", "frontend"):
        assert len((run_dir / f"{unit_id}-probes").read_text().splitlines()) >= 5
        assert health[unit_id]["last_verdict"] == "alive"


def test_health_stop_refusal_preserves_root_and_serving_siblings(short_tmp: Path) -> None:
    """Real root/children and HTTP representatives; no gateway application is booted."""
    run_dir, ready, release = short_tmp / "run", short_tmp / "ready", short_tmp / "release"
    child_script = Path(__file__).with_name("child_unit.py")
    units: list[dict[str, object]] = [
        {
            "id": "agent-host",
            "exec": [sys.executable, str(child_script), "restart_down", str(ready), str(release)],
            "restart": "always",
            "stop_timeout_s": 0.1,
        }
    ]
    (short_tmp / "reply").write_text("still serving")
    for unit_id in ("gateway", "frontend"):
        units.append(
            {
                "id": unit_id,
                "restart": "always",
                "exec": [
                    sys.executable,
                    "-u",
                    "-m",
                    "http.server",
                    "0",
                    "--bind",
                    "127.0.0.1",
                    "--directory",
                    str(short_tmp),
                ],
            }
        )
    manifests = _write_manifests(short_tmp, units)
    with _daemon(run_dir, manifests, wiring=_wiring("wiring_refused_health")) as (proc, log):
        client = _wait_ready(run_dir, proc, log)
        original_child = OwnedProcess.capture(
            psutil.Process(cast(int, _units_of(client.status())[0]["pid"]))
        )
        try:
            _wait_for(ready.exists, "child did not install its TERM handler")
            _wait_for(
                lambda: all(_serving_port(run_dir, name) for name in ("gateway", "frontend")),
                "representative services did not start",
            )
            _assert_serving_siblings(run_dir)
            initial = cast("dict[str, object]", client.status().get("result"))
            (run_dir / "fail-health").touch()
            _wait_for(
                lambda: (
                    proc.poll() is not None
                    or _read_log(log).count("failure state restart_failed") >= 5
                ),
                "health rounds stopped after a unit refused TERM",
            )
            assert proc.poll() is None, _read_log(log)
            current = cast("dict[str, object]", client.status().get("result"))
            assert _status_births(current) == _status_births(initial)
            _assert_refused_generation(current)
            _assert_budget_and_observation(run_dir, current)
            _assert_serving_siblings(run_dir)
            release.touch()
            _wait_for(
                lambda: _units_of(client.status())[0]["state"] == "stopped",
                "refused generation was not reaped after release",
            )
            assert client.restart("agent-host")["ok"]
            repaired = _units_of(client.status())[0]
            assert repaired["restart_failed"] is None and repaired["state"] == "running"
        finally:
            release.touch()
            # Keep the sentinel until the child consumes it, even if root failed first.
            _wait_for(lambda: not original_child.live(), "released child did not exit")
    assert proc.returncode == 0, _read_log(log)
