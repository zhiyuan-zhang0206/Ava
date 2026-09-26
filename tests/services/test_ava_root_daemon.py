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
import stat
import subprocess
import sys
import time
from collections.abc import Callable, Generator
from contextlib import contextmanager
from pathlib import Path
from typing import cast

import psutil
import pytest

from shared.os_boot_unit import BootUnitContext
from shared.root_control.client import RootClient, RootClientError
from shared.root_control.ipc import ResponsePayload

REPO_ROOT = Path(__file__).resolve().parents[2]

_SLEEPER = [sys.executable, "-u", "-c", "import time; time.sleep(300)"]

_SOCKET_NAME = "ava-root.sock"


def _write_manifests(base: Path, units: list[dict[str, object]]) -> Path:
    path = base / "units.json"
    path.write_text(json.dumps({"units": units}), encoding="utf-8")
    return path


def _daemon_command(run_dir: Path, manifests: Path, *, wiring: str | None = None) -> list[str]:
    command = [
        sys.executable,
        "-m",
        "services.ava_root",
        "--run-dir",
        str(run_dir),
        "--manifests",
        str(manifests),
    ]
    if wiring is not None:
        command.extend(["--wiring", wiring])
    return command


@contextmanager
def _daemon(
    run_dir: Path,
    manifests: Path,
    *,
    wiring: str | None = None,
    env: dict[str, str] | None = None,
) -> Generator[tuple[subprocess.Popen[bytes], Path]]:
    """Run a daemon; guarantee it is gone (TERM, then KILL) at scope exit."""
    run_dir.mkdir(parents=True, exist_ok=True)
    log_path = run_dir / "daemon.log"
    with log_path.open("ab") as log_file:
        proc = subprocess.Popen(  # noqa: S603 — fixed argv, this module's own command, no shell
            _daemon_command(run_dir, manifests, wiring=wiring),
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


def test_daemon_end_to_end(short_tmp: Path) -> None:
    run_dir = short_tmp / "nested" / "run"  # the daemon creates its run dir
    manifests = _write_manifests(short_tmp, [{"id": "svc", "exec": _SLEEPER, "restart": "always"}])
    with _daemon(run_dir, manifests) as (proc, log_path):
        client = _wait_ready(run_dir, proc, log_path)
        # K3 face: the control socket is owner-only.
        assert stat.S_IMODE((run_dir / _SOCKET_NAME).stat().st_mode) == 0o600

        response = client.status()
        result = cast("dict[str, object]", response.get("result"))
        root = cast("dict[str, object]", result["root"])
        assert root["pid"] == proc.pid
        units = _units_of(response)
        assert [u["id"] for u in units] == ["svc"]
        assert units[0]["state"] == "running"
        unit_pid = cast(int, units[0]["pid"])
        assert (run_dir / "logs" / "svc" / "output.log").exists()

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


def test_failed_shutdown_retains_owner_and_requires_explicit_closure(short_tmp: Path) -> None:
    """A stubborn real child cannot turn ordinary root stop into orphaning or force."""
    ready = short_tmp / "child-ready"
    command = [
        sys.executable,
        "-u",
        "-c",
        (
            "import signal,time,pathlib; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
            f"pathlib.Path({str(ready)!r}).write_text('ready'); time.sleep(300)"
        ),
    ]
    manifests = _write_manifests(short_tmp, [{"id": "svc", "exec": command, "restart": "never"}])
    fixture = short_tmp / "fixtures"
    fixture.mkdir()
    (fixture / "short_deadline.py").write_text(
        "from services.ava_root.supervisor import SupervisorConfig\n"
        "def build(context):\n"
        "    context.supervisor._config = SupervisorConfig(stop_timeout_s=0.15)\n"
        "    return []\n"
    )
    run_dir = short_tmp / "run"
    with _daemon(
        run_dir,
        manifests,
        wiring="short_deadline:build",
        env=_wiring_env(fixture, short_tmp / "markers"),
    ) as (root, log):
        client = _wait_ready(run_dir, root, log)
        _wait_for(ready.exists, "child did not install its TERM handler")
        before = _units_of(client.status())[0]
        child = cast(int, before["pid"])
        try:
            assert client.shutdown()["ok"]
            _wait_for(
                lambda: "retains custody after failed shutdown" in _read_log(log),
                "root did not report retained custody after its shutdown deadline",
            )
            assert root.poll() is None
            _assert_alive(child)
            assert (run_dir / "custody/svc.json").exists()
            for _ in range(2):
                observed = _units_of(client.status())[0]
                assert (observed["pid"], observed["create_time"]) == (child, before["create_time"])
            refusals = [
                client.up("svc"),
                client.restart("svc"),
                client.resource("terminal.start", {}),
            ]
            assert all(not response["ok"] for response in refusals)
            assert client.force_down("svc")["ok"]
            _wait_dead(child)
            assert not list((run_dir / "custody").iterdir())
            assert client.shutdown()["ok"]
            assert root.wait(timeout=5) == 0
        finally:
            _kill_quietly(child)


def test_lock_dies_with_the_daemon_and_is_not_inherited_by_units(short_tmp: Path) -> None:
    """A released lock is not permission to duplicate orphaned application services."""
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

            # The free lock cannot prove the former unit lineage is gone.
            # A replacement must hold without spawning a competing generation.
            with _daemon(run_dir, manifests) as (proc_b, log_b):
                assert proc_b.wait(timeout=10) != 0
                assert "custody requires reconciliation" in _read_log(log_b)
                _assert_alive(unit_pid)
                assert (run_dir / "custody/svc.json").exists()
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


_WIRING_FIXTURE = '''\
"""Recording wiring participant for the daemon wiring tests (written to tmp)."""

import os
from pathlib import Path


def build(context):
    markers = Path(os.environ["AVA_ROOT_WIRING_MARKERS"])
    markers.mkdir(parents=True, exist_ok=True)

    class Participant:
        def start(self):
            (markers / "started").write_text("yes", encoding="utf-8")

        def stop(self):
            (markers / "stopped").write_text("yes", encoding="utf-8")

    return [Participant()]
'''


def _wiring_env(fixture_dir: Path, markers: Path) -> dict[str, str]:
    env = dict(os.environ)
    existing = env.get("PYTHONPATH")
    env["PYTHONPATH"] = str(fixture_dir) if not existing else f"{fixture_dir}{os.pathsep}{existing}"
    env["AVA_ROOT_WIRING_MARKERS"] = str(markers)
    return env


def test_wiring_hook_start_stop_lifecycle(short_tmp: Path) -> None:
    run_dir = short_tmp / "run"
    manifests = _write_manifests(short_tmp, [{"id": "svc", "exec": _SLEEPER, "restart": "always"}])
    fixture_dir = short_tmp / "fixtures"
    fixture_dir.mkdir()
    (fixture_dir / "record_wiring.py").write_text(_WIRING_FIXTURE, encoding="utf-8")
    markers = short_tmp / "markers"
    with _daemon(
        run_dir, manifests, wiring="record_wiring:build", env=_wiring_env(fixture_dir, markers)
    ) as (proc, log_path):
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
) -> Path:
    """A disposable readiness caller for the real generic root, not a deployed wrapper."""
    starter = receipt.parent / "start.py"
    starter.write_text(
        "import json, os, subprocess, sys, time\n"
        "from pathlib import Path\n"
        f"sys.path.insert(0, {str(REPO_ROOT)!r})\n"
        "import psutil\n"
        "from shared.root_control.client import RootClient, RootClientError, native_identity\n"
        "from shared.os_boot_unit import publish_root_ready, root_pid_path\n"
        "from shared.native_process.ownership import OwnedProcess\n"
        "from dataclasses import asdict\n"
        f"receipt = Path({str(receipt)!r})\n"
        f"assert os.environ['AVA_CLUSTER_REGISTRY'] == {str(home.parent / 'registry.json')!r}\n"
        f"data = subprocess.Popen({_SLEEPER!r}, start_new_session=True)\n"
        f"hint = root_pid_path(Path({str(home)!r}))\n"
        "hint.parent.mkdir(parents=True, exist_ok=True)\n"
        "hint.write_text(str(data.pid) + '\\n')\n"
        "births = {'data': asdict(OwnedProcess.capture(psutil.Process(data.pid)))}\n"
        "receipt.write_text(json.dumps(births))\n"
        f"root = subprocess.Popen({_daemon_command(run_dir, manifest)!r}, start_new_session=True)\n"
        "births['root'] = asdict(OwnedProcess.capture(psutil.Process(root.pid)))\n"
        "receipt.write_text(json.dumps(births))\n"
        f"client = RootClient(Path({str(run_dir / _SOCKET_NAME)!r}), timeout=1)\n"
        "deadline = time.monotonic() + 20\n"
        "while True:\n"
        "    try:\n"
        "        status = client.status()['result']\n"
        "        if status['units'][0]['state'] == 'running': break\n"
        "    except (RootClientError, KeyError): pass\n"
        "    if root.poll() is not None or time.monotonic() > deadline: raise RuntimeError('root unready')\n"
        "    time.sleep(.05)\n"
        "births['app'] = asdict(native_identity(status['units'][0]))\n"
        "receipt.write_text(json.dumps(births))\n"
        + (
            "receipt.with_suffix('.failure').write_text('before')\nraise RuntimeError('failed before publication')\n"
            if failure == "before"
            else ""
        )
        + f"publish_root_ready(Path({str(home)!r}), native_identity(status['root']))\n"
        + (
            "receipt.with_suffix('.failure').write_text('after')\nraise RuntimeError('failed after publication')\n"
            if failure == "after"
            else ""
        )
    )
    return starter


def _terminate_systemd_test_births(receipt: Path) -> None:
    from shared.native_process.ownership import OwnedProcess

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
        home.parent / "registry.json",
    )


def _assert_failed_adoption(ctx: BootUnitContext, receipt: Path, failure: str) -> None:
    from shared import os_boot_unit

    assert receipt.with_suffix(".failure").read_text() == failure
    births = json.loads(receipt.read_text())
    assert set(births) == {"data", "root", "app"}
    assert os_boot_unit._manager_properties(ctx.home)["MainPID"] == "0"
    # Before publication the deliberately stale hint names data; afterwards it
    # names root. Neither becomes MainPID when the ordinary starter fails.
    expected = births["data" if failure == "before" else "root"]["pid"]
    hint = os_boot_unit.root_pid_path(ctx.home)
    if hint.exists():  # Native manager may remove the failed unit's hint.
        assert hint.read_text().strip() == str(expected)


def _require_native_systemd() -> None:
    from shared import os_boot_unit

    if sys.platform != "linux" or not os_boot_unit.systemd_running():
        pytest.skip("native Linux systemd required")
    allowed = subprocess.run(["sudo", "-n", "true"], check=False, timeout=10)
    if allowed.returncode:
        pytest.skip("native systemd test requires passwordless sudo")


@pytest.mark.parametrize("failure", ["", "before", "after"])
def test_native_systemd_root_lifetime(tmp_path: Path, failure: str) -> None:
    """Actual manager handoff, root TERM closure, and data sibling retention.

    The retained sleeper represents a separately owned native data process;
    this does not claim database protocol or durability verification. The
    dedicated Ubuntu CI step asserts systemd and sudo before invoking this test.
    """
    from shared import os_boot_unit

    _require_native_systemd()
    ctx = _systemd_test_context(tmp_path / "home")
    ctx.home.mkdir()
    run_dir = ctx.home / "root"
    manifest = _write_manifests(tmp_path, [{"id": "app", "exec": _SLEEPER, "restart": "always"}])
    receipt = tmp_path / "births.json"
    starter = _systemd_starter(ctx.home, receipt, run_dir, manifest, failure)
    unit = os_boot_unit.unit_name(ctx.home)
    target = Path("/run/systemd/system") / unit
    holder = tmp_path / unit
    content = os_boot_unit.render_unit(ctx)
    exec_line = next(row for row in content.splitlines() if row.startswith("ExecStart="))
    replacement = f"ExecStart=:{os_boot_unit._quote(sys.executable, 'python')} {os_boot_unit._quote(str(starter), 'test starter')}"
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
        from shared.native_process.ownership import OwnedProcess

        owners = {key: OwnedProcess(**value) for key, value in raw.items()}
        root, app, data = owners["root"], owners["app"], owners["data"]
        manager = os_boot_unit._manager_properties(ctx.home)
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
