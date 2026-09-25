"""Durable outer-owner intent and isolated signed-artifact preparation."""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from services.ava_root.custody import ServiceCustody, require_clear
from services.permissions_helper import lifecycle


def test_pending_birth_survives_process_owner_loss(tmp_path: Path) -> None:
    ServiceCustody(tmp_path, "gateway")
    with pytest.raises(RuntimeError, match="requires reconciliation"):
        require_clear(tmp_path)
    with pytest.raises(FileExistsError):
        ServiceCustody(tmp_path, "gateway")


def test_isolated_helper_build_never_replaces_installed_artifact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    installed = tmp_path / "installed"
    installed.mkdir()
    sentinel = installed / "build-state.json"
    sentinel.write_text("unchanged installed state")
    candidate = tmp_path / "candidate"
    monkeypatch.setattr(lifecycle, "_BUILD_DIR", installed)
    monkeypatch.setattr(lifecycle, "_source_content_hash", lambda: "source")
    monkeypatch.setattr(lifecycle, "_expected_dr", lambda: "stable-dr")

    def verify(_app: Path) -> str:
        return "stable-dr"

    monkeypatch.setattr(lifecycle, "_verify_dr", verify)
    monkeypatch.setattr(lifecycle, "_keychain_lock_reason", lambda: None)
    monkeypatch.setattr(lifecycle, "_interactive_signing_reason", lambda: None)
    monkeypatch.setattr(lifecycle, "preflight_signing_smoke", lambda: None)

    commands: list[list[str]] = []

    def run(command: list[str]) -> subprocess.CompletedProcess[bytes]:
        commands.append(command)
        if command[0] == "swiftc":
            Path(command[-1]).write_bytes(b"test executable")
        return subprocess.CompletedProcess(command, 0, b"", b"")

    monkeypatch.setattr(lifecycle, "_run", run)
    app, rebuilt = lifecycle.build_and_sign(destination=candidate)
    assert rebuilt and app == candidate / "AvaPermissionsHelper.app"
    assert sentinel.read_text() == "unchanged installed state"
    assert sorted(path.name for path in installed.iterdir()) == ["build-state.json"]
    assert (candidate / "build-state.json").is_file()
    assert [command[0] for command in commands] == ["swiftc", "codesign"]
    assert all(str(installed) not in argument for command in commands for argument in command)


def test_isolated_helper_rejects_installed_destination_before_signing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(lifecycle, "_BUILD_DIR", tmp_path)
    with pytest.raises(lifecycle.PermissionsHelperBuildError, match="outside the installed"):
        lifecycle.build_and_sign(destination=tmp_path)


@pytest.mark.skipif(
    sys.platform != "darwin" or shutil.which("swift") is None,
    reason="native Swift filesystem intent contract",
)
def test_helper_stop_intent_survives_new_reader_and_refuses_corruption(tmp_path: Path) -> None:
    source = lifecycle._SOURCE.read_text()
    implementation = source.split("enum StopOwner", 1)[1].split("func helperRunDir", 1)[0]
    program = tmp_path / "intent.swift"
    program.write_text(
        "import Foundation\nimport Darwin\nenum OpError: Error { case bad(String) }\n"
        + "enum StopOwner"
        + implementation
        + "\nlet directory = CommandLine.arguments[1]\n"
        + "try StopIntent.store(directory, owner: .root)\n"
        + "let retained = try StopIntent.exists(directory, owner: .root); assert(retained)\n"
        + "try StopIntent.store(directory, owner: .root)\n"
        + "try StopIntent.clear(directory, owner: .root)\n"
        + "let cleared = try StopIntent.exists(directory, owner: .root); assert(!cleared)\n"
        + 'try "corrupt".write(toFile: StopIntent.path(directory, owner: .root), atomically: true, encoding: .utf8)\n'
        + 'do { _ = try StopIntent.exists(directory, owner: .root); fatalError("corrupt intent admitted") } catch {}\n'
    )
    result = subprocess.run(  # noqa: S603 — exact repository-owned Swift fragment in a private temporary directory
        ["swift", str(program), str(tmp_path)],
        capture_output=True,
        text=True,
        timeout=45,
        check=False,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.skipif(
    sys.platform != "darwin" or shutil.which("swift") is None,
    reason="native Swift child ownership contract",
)
def test_helper_native_reaping_cannot_race_owned_signal_delivery(tmp_path: Path) -> None:
    source = lifecycle._SOURCE.read_text()
    # Compile the actual spawn/session and root-keeper implementations without
    # the GUI server. Test barriers widen the old race, without runtime hooks.
    children = source.split("/// Resolve an allowed path", 1)[0]
    keeper = source.split("// MARK: - Root keeper", 1)[1].split("// MARK: - Dispatch", 1)[0]
    implementation = (children + keeper).replace("waitpid(", "observedWaitpid(")
    implementation = implementation.replace(
        "kill(child, SIGTERM)", "pausedRootSignal(child, SIGTERM)"
    ).replace("kill(child.pid, signalValue)", "pausedSessionSignal(child.pid, signalValue)")
    implementation = implementation.replace(
        "try StopIntent.store(runDir, owner: .helper)", "try pausedHelperStopIntent(runDir)"
    )
    fixture = Path(__file__).with_name("helper_child_custody.swift").read_text()
    program = tmp_path / "custody.swift"
    program.write_text(implementation + fixture)
    result = subprocess.run(  # noqa: S603 — private native fixture, no daemon socket, signing or scheduler
        ["swift", str(program), str(tmp_path)],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "foreign-child isolation passed" in result.stdout
    assert "helper shutdown admission passed" in result.stdout
    # A separate process loses its controller immediately after durable intent;
    # the next process must observe it before any root or session birth.
    crash_home = tmp_path / "crash-home"
    crash_home.mkdir()
    for mode, expected in (("crash-after-intent", 73), ("restart-after-intent", 0)):
        crash = subprocess.run(  # noqa: S603 — same private native fixture
            ["swift", str(program), str(crash_home), mode],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        assert crash.returncode == expected, crash.stdout + crash.stderr


def test_mutated_custody_is_never_overwritten_or_cleared(tmp_path: Path) -> None:
    record = ServiceCustody(tmp_path, "worker")
    record.path.write_text("replacement authority")
    for operation in (lambda: record.retain(set()), record.clear):
        with pytest.raises(RuntimeError, match="custody changed"):
            operation()
        assert record.path.read_text() == "replacement authority"


@pytest.mark.parametrize("birth", [float("nan"), float("inf"), -1, True])
def test_native_status_refuses_invalid_birth(birth: object) -> None:
    from services.ava_root.client import RootClientError, native_identity

    with pytest.raises(RootClientError, match="captured native birth"):
        native_identity({"pid": 101, "create_time": birth, "starttime": None})


@pytest.mark.parametrize("waiting", [b"", b"pid = 0\n"])
def test_exact_home_helper_retirement_keeps_neighbor_and_waits_for_native_exit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, waiting: bytes
) -> None:
    from services.permissions_helper import launchd_job as jobs
    from shared.proc_tree import OwnedProcess

    home = tmp_path.resolve() / "home"
    agents = tmp_path / "agents"
    agents.mkdir()
    monkeypatch.setattr(jobs.sys, "platform", "darwin")
    monkeypatch.setattr(jobs, "helper_job_agents_dir", lambda: agents)
    path = jobs.helper_job_plist_path(home)
    _stopped_helper_plist(home, path)
    neighbor = agents / "neighbor.plist"
    neighbor.write_bytes(b"neighbor authority")
    alive = [True]
    owner = OwnedProcess(321, 100.0, None)

    def capture(_state: str, _socket: Path, _executable: str) -> OwnedProcess:
        return owner

    monkeypatch.setattr(jobs, "_retirement_owner", capture)
    commands: list[list[str]] = []
    loaded = [True]

    from services.permissions_helper import client

    def shutdown(run_dir: Path, *, sock_path: str | Path) -> client.HelperShutdownResult:
        assert run_dir == home / "run" / "ava-root"
        marker = run_dir / "helper-stopped"
        marker.write_bytes(b"stopped\n")
        marker.chmod(0o600)
        alive[0] = False
        return {"stopping": True, "pid": owner.pid, "run_dir": str(run_dir)}

    def wait(captured: OwnedProcess, _deadline: float) -> None:
        assert captured == owner and not alive[0]

    monkeypatch.setattr(client, "shutdown_helper", shutdown)
    monkeypatch.setattr(jobs, "_wait_retirement_owner", wait)

    def command(args: list[str], _deadline: float) -> subprocess.CompletedProcess[bytes]:
        commands.append(args)
        if args[0] == "bootout":
            assert not alive[0], "bootout must wait for the captured native helper's exit"
            loaded[0] = False
            return subprocess.CompletedProcess(args, 0, b"", b"")
        if alive[0]:
            return subprocess.CompletedProcess(args, 0, b"pid = 321\n", b"")
        if loaded[0]:
            return subprocess.CompletedProcess(
                args, 0, b"state = not running\nlast exit code = 0\n" + waiting, b""
            )
        return subprocess.CompletedProcess(args, 113, b"", b"Could not find service")

    monkeypatch.setattr(jobs, "_retirement_command", command)
    # Ordinary stop, then the shared destroy boundary, then a cleanup retry.
    from cli.commands._stop_extras import stop_permissions_helper
    from shared.config import settings

    monkeypatch.setattr("shared.platform.IS_MACOS", True)
    monkeypatch.setattr("shared.paths.ava_home", lambda: home)
    monkeypatch.setattr(settings.services, "permissions_helper_port", 23456)
    stop_permissions_helper()
    jobs.unregister_helper(home, helper_port=23456)
    jobs.unregister_helper(home, helper_port=23456)
    assert not path.exists()
    assert neighbor.read_bytes() == b"neighbor authority"
    expected = f"{jobs.helper_job_domain()}/{jobs.helper_job_label(home)}"
    assert all(args[1] == expected for args in commands)
    assert [args[0] for args in commands].count("bootout") == 1
    assert all(args[0] in ("print", "bootout") for args in commands)


def test_helper_retirement_unknown_job_never_removes_definition(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from services.permissions_helper import launchd_job as jobs

    home = tmp_path.resolve()
    monkeypatch.setattr(jobs.sys, "platform", "darwin")
    monkeypatch.setattr(jobs, "helper_job_agents_dir", lambda: tmp_path)
    path = jobs.helper_job_plist_path(home)
    path.write_bytes(b"retained definition")

    def unknown(args: list[str], _deadline: float) -> subprocess.CompletedProcess[bytes]:
        assert args[0] == "print"
        return subprocess.CompletedProcess(args, 5, b"", b"native manager unavailable")

    monkeypatch.setattr(jobs, "_retirement_command", unknown)
    with pytest.raises(RuntimeError, match="cannot inspect"):
        jobs.unregister_helper(home, helper_port=23456)
    assert path.read_bytes() == b"retained definition"


def test_helper_retirement_rejects_caller_ancestry_before_wire_or_signal(tmp_path: Path) -> None:
    import os

    import psutil

    from services.permissions_helper import launchd_job as jobs

    with pytest.raises(RuntimeError, match="ancestor"):
        jobs._retirement_owner(
            f"pid = {os.getpid()}\n", tmp_path / "no-socket", psutil.Process().exe()
        )


def test_helper_retirement_refuses_foreign_home_plist(tmp_path: Path) -> None:
    import plistlib

    from services.permissions_helper import launchd_job as jobs

    home = tmp_path.resolve()
    path = tmp_path / "job.plist"
    path.write_bytes(
        plistlib.dumps(
            {
                "Label": jobs.helper_job_label(home),
                "ProgramArguments": ["/test/helper"],
                "EnvironmentVariables": {"AVA_PERMISSIONS_HELPER_SOCKET": "/foreign/home/socket"},
            }
        )
    )
    with pytest.raises(RuntimeError, match="exact home"):
        jobs._retirement_plist(path, home, home / "socket")


def _stopped_helper_plist(home: Path, path: Path) -> bytes:
    import plistlib

    from services.permissions_helper import launchd_job as jobs

    body = plistlib.dumps(
        {
            "Label": jobs.helper_job_label(home),
            "ProgramArguments": ["/private/test/AvaPermissionsHelper"],
            "KeepAlive": {"SuccessfulExit": False},
            "EnvironmentVariables": {
                "AVA_PERMISSIONS_HELPER_SOCKET": str(home / "run/permissions-helper.23456.sock"),
                "AVA_PERMISSIONS_HELPER_ROOT_SEED": str(home / "run/ava-root/seed.json"),
            },
        }
    )
    path.write_bytes(body)
    return body


@pytest.mark.parametrize("conflict", [None, "reappeared", "changed", "live-socket", "wrong-seed"])
def test_retained_definition_after_completed_stop_requires_exact_absence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, conflict: str | None
) -> None:
    import plistlib

    from services.permissions_helper import launchd_job as jobs

    # Keep the real UNIX socket below the native sockaddr_un path limit.
    with tempfile.TemporaryDirectory(prefix="avah-", dir="/tmp") as directory:
        home = Path(directory).resolve()
        (home / "run").mkdir()
        monkeypatch.setattr(jobs.sys, "platform", "darwin")
        monkeypatch.setattr(jobs, "helper_job_agents_dir", lambda: tmp_path)
        path = jobs.helper_job_plist_path(home)
        _stopped_helper_plist(home, path)
        if conflict == "wrong-seed":
            data = plistlib.loads(path.read_bytes())
            data["EnvironmentVariables"]["AVA_PERMISSIONS_HELPER_ROOT_SEED"] = "/foreign/seed.json"
            path.write_bytes(plistlib.dumps(data))
        neighbor = tmp_path / "neighbor.plist"
        neighbor.write_bytes(b"neighbor")
        reads: list[str] = []

        def query(target: str, _deadline: float) -> str | None:
            assert target.endswith(jobs.helper_job_label(home))
            reads.append(target)
            if len(reads) == 3:
                if conflict == "reappeared":
                    return "pid = 321\n"
                if conflict == "changed":
                    path.write_bytes(b"changed authority")
            return None

        monkeypatch.setattr(jobs, "_retirement_query", query)
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
            sock = home / "run/permissions-helper.23456.sock"
            listener.bind(str(sock))
            if conflict == "live-socket":
                listener.listen()
            if conflict is None:
                jobs.unregister_helper(home, helper_port=23456)
                jobs.unregister_helper(home, helper_port=23456)
                assert not path.exists() and len(reads) == 5
            else:
                with pytest.raises(RuntimeError):
                    jobs.unregister_helper(home, helper_port=23456)
                assert path.exists()
        assert neighbor.read_bytes() == b"neighbor"


def test_helper_socket_inspection_error_retains_definition(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from services.permissions_helper import launchd_job as jobs

    home = tmp_path.resolve()
    monkeypatch.setattr(jobs.sys, "platform", "darwin")
    monkeypatch.setattr(jobs, "helper_job_agents_dir", lambda: tmp_path)
    path = jobs.helper_job_plist_path(home)
    original = _stopped_helper_plist(home, path)
    (home / "run").mkdir()
    # A regular file at the socket address is unknown custody, never absence.
    (home / "run/permissions-helper.23456.sock").write_bytes(b"untrusted socket input")

    def absent(_target: str, _deadline: float) -> str | None:
        return None

    monkeypatch.setattr(jobs, "_retirement_query", absent)
    with pytest.raises(RuntimeError, match="socket absence"):
        jobs.unregister_helper(home, helper_port=23456)
    assert path.read_bytes() == original


@pytest.mark.skipif(sys.platform == "win32", reason="helper native POSIX stop contract")
def test_helper_native_shutdown_wait_never_signals_unresponsive_owner(tmp_path: Path) -> None:
    import psutil

    from services.permissions_helper import launchd_job as jobs
    from shared.proc_tree import OwnedProcess

    ready = tmp_path / "ready"
    code = (
        "import pathlib,signal,time\n"
        "signal.signal(signal.SIGTERM, lambda *_: None)\n"
        f"pathlib.Path({str(ready)!r}).touch()\n"
        "time.sleep(60)\n"
    )
    child = subprocess.Popen([sys.executable, "-c", code])  # noqa: S603 — disposable test child
    try:
        deadline = time.monotonic() + 10
        while not ready.exists():
            assert child.poll() is None and time.monotonic() < deadline
            time.sleep(0.01)
        owner = OwnedProcess.capture(psutil.Process(child.pid))
        with pytest.raises(TimeoutError, match="did not complete"):
            jobs._wait_retirement_owner(owner, time.monotonic() + 0.05)
        assert owner.live(), "normal helper shutdown wait must not signal its owner"
        jobs._force_retirement_owner(owner, time.monotonic() + 5)
        assert not owner.live()
    finally:
        if child.poll() is None:
            child.kill()
        child.wait(timeout=5)


def test_helper_force_signal_refuses_reused_native_birth(monkeypatch: pytest.MonkeyPatch) -> None:
    import psutil

    from services.permissions_helper import launchd_job as jobs
    from shared.proc_tree import OwnedProcess

    original = OwnedProcess(12345, 100.0, None)
    replacement = OwnedProcess(12345, 100.01, None)
    signals: list[str] = []

    class NativeProcess:
        def kill(self) -> None:
            signals.append("kill")

    def process(_pid: int) -> NativeProcess:
        return NativeProcess()

    def capture(_process: object) -> OwnedProcess:
        return replacement

    monkeypatch.setattr(psutil, "Process", process)
    monkeypatch.setattr(OwnedProcess, "capture", staticmethod(capture))
    with pytest.raises(RuntimeError, match="identity changed"):
        jobs._force_retirement_owner(original, time.monotonic() + 5)
    assert signals == []


@pytest.mark.parametrize("fault", [None, "no-intent", "corrupt-intent", "running", "failed-exit"])
def test_idle_shutdown_retry_requires_native_success_and_retained_intent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fault: str | None
) -> None:
    from services.permissions_helper import launchd_job as jobs

    home = tmp_path.resolve()
    monkeypatch.setattr(jobs.sys, "platform", "darwin")
    monkeypatch.setattr(jobs, "helper_job_agents_dir", lambda: tmp_path)
    path = jobs.helper_job_plist_path(home)
    original = _stopped_helper_plist(home, path)
    marker = home / "run" / "ava-root" / "helper-stopped"
    marker.parent.mkdir(parents=True)
    if fault != "no-intent":
        marker.write_bytes(b"corrupt" if fault == "corrupt-intent" else b"stopped\n")
        marker.chmod(0o600)
    commands: list[list[str]] = []
    loaded = [True]

    def command(args: list[str], _deadline: float) -> subprocess.CompletedProcess[bytes]:
        commands.append(args)
        if args[0] == "bootout":
            loaded[0] = False
            return subprocess.CompletedProcess(args, 0, b"", b"")
        if not loaded[0]:
            return subprocess.CompletedProcess(args, 113, b"", b"Could not find service")
        state = "spawn scheduled" if fault == "running" else "not running"
        code = 1 if fault == "failed-exit" else 0
        return subprocess.CompletedProcess(
            args, 0, f"state = {state}\nlast exit code = {code}\n".encode(), b""
        )

    monkeypatch.setattr(jobs, "_retirement_command", command)
    if fault is None:
        jobs.unregister_helper(home, helper_port=23456)
        jobs.unregister_helper(home, helper_port=23456)
        assert not path.exists() and marker.exists()
        assert sum(args[0] == "bootout" for args in commands) == 1
    else:
        with pytest.raises(RuntimeError):
            jobs.unregister_helper(home, helper_port=23456)
        assert path.read_bytes() == original
        assert all(args[0] == "print" for args in commands)


@pytest.mark.parametrize("fault", ["wrong-ack", "lost-ack", "relaunch", "missing-intent"])
def test_unproven_shutdown_never_boots_out_native_job(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fault: str
) -> None:
    from services.permissions_helper import client
    from services.permissions_helper import launchd_job as jobs
    from shared.proc_tree import OwnedProcess

    home = tmp_path.resolve()
    monkeypatch.setattr(jobs.sys, "platform", "darwin")
    monkeypatch.setattr(jobs, "helper_job_agents_dir", lambda: tmp_path)
    path = jobs.helper_job_plist_path(home)
    original = _stopped_helper_plist(home, path)
    owner = OwnedProcess(321, 100.0, None)
    requested = [False]
    commands: list[list[str]] = []

    def capture(_state: str, _socket: Path, _executable: str) -> OwnedProcess:
        return owner

    def wait(_owner: OwnedProcess, _deadline: float) -> None:
        pass

    def shutdown(run_dir: Path, *, sock_path: str | Path) -> client.HelperShutdownResult:
        requested[0] = True
        if fault != "missing-intent":
            marker = run_dir / "helper-stopped"
            marker.write_bytes(b"stopped\n")
            marker.chmod(0o600)
        if fault == "lost-ack":
            raise RuntimeError("lost response after durable intent")
        return {
            "stopping": True,
            "pid": 999 if fault == "wrong-ack" else 321,
            "run_dir": str(run_dir),
        }

    def command(args: list[str], _deadline: float) -> subprocess.CompletedProcess[bytes]:
        commands.append(args)
        assert args[0] == "print", "unknown shutdown must retain native job authority"
        state = "pid = 321\n"
        if requested[0]:
            state = (
                "pid = 999\n"
                if fault == "relaunch"
                else "state = not running\nlast exit code = 0\n"
            )
        return subprocess.CompletedProcess(args, 0, state.encode(), b"")

    monkeypatch.setattr(jobs, "_retirement_owner", capture)
    monkeypatch.setattr(jobs, "_wait_retirement_owner", wait)
    monkeypatch.setattr(jobs, "_retirement_command", command)
    monkeypatch.setattr(client, "shutdown_helper", shutdown)
    with pytest.raises(RuntimeError):
        jobs.unregister_helper(home, helper_port=23456)
    assert requested[0] and path.read_bytes() == original
    assert all(args[0] == "print" for args in commands)


@pytest.mark.skipif(
    sys.platform != "darwin" or shutil.which("swiftc") is None,
    reason="native serial helper request and Foundation child contract",
)
def test_helper_shutdown_waits_for_inflight_gui_child_before_acknowledging() -> None:
    # Compile the complete daemon and use its actual socket/dispatch/wait path.
    # Replace only desktop effects with a controlled child; never register TCC,
    # sign an artifact, load a native job, or invoke the real screenshot tool.
    with tempfile.TemporaryDirectory(prefix="ava-hreq-", dir="/tmp") as directory:
        home = Path(directory).resolve()
        run_dir = home / "run"
        run_dir.mkdir()
        sock = home / "helper.sock"
        ready, release = home / "child-ready", home / "child-release"
        script = home / "child.sh"
        script.write_text('touch "$1"\nwhile [ ! -e "$2" ]; do sleep 0.01; done\n')
        source = lifecycle._SOURCE.read_text().replace("    reportResponsiblePID()\n", "")
        source = source.replace("    registerPermissions()\n", "")
        source = source.replace(
            'URL(fileURLWithPath: "/usr/sbin/screencapture")', 'URL(fileURLWithPath: "/bin/sh")'
        )
        source = source.replace(
            '["-x", "-R\\(x),\\(y),\\(w),\\(h)", path]',
            json.dumps([str(script), str(ready), str(release)]),
        )
        assert 'p.arguments = ["' + str(script) in source
        program, executable = home / "main.swift", home / "helper"
        program.write_text(source)
        compiled = subprocess.run(  # noqa: S603 — private unsigned native fixture
            ["swiftc", str(program), "-o", str(executable)],
            capture_output=True,
            timeout=30,
            check=False,
        )
        assert compiled.returncode == 0, compiled.stderr.decode()

        def request(method: str, **arguments: object) -> dict[str, object]:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
                connection.settimeout(5)
                connection.connect(str(sock))
                connection.sendall(
                    json.dumps({"id": 1, "method": method, **arguments}).encode() + b"\n"
                )
                return json.loads(connection.makefile("rb").readline())

        environment = dict(os.environ)
        environment.update(
            AVA_PERMISSIONS_HELPER_SOCKET=str(sock),
            AVA_PERMISSIONS_HELPER_ROOT_SEED=str(run_dir / "seed.json"),
        )
        process = subprocess.Popen(  # noqa: S603 — private unsigned fixture without OS registration
            [str(executable)], env=environment, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
        try:
            deadline = time.monotonic() + 10
            while not sock.exists():
                assert process.poll() is None and time.monotonic() < deadline
                time.sleep(0.01)
            with ThreadPoolExecutor(max_workers=2) as pool:
                capture = pool.submit(
                    request,
                    "screencapture_region",
                    x=0,
                    y=0,
                    w=1,
                    h=1,
                    path=str(home / "image.png"),
                )
                while not ready.exists():
                    assert not capture.done() and time.monotonic() < deadline
                    time.sleep(0.01)
                shutdown = pool.submit(request, "helper_shutdown", run_dir=str(run_dir))
                time.sleep(0.1)
                assert not shutdown.done() and not (run_dir / "helper-stopped").exists()
                release.touch()
                assert capture.result(timeout=5)["ok"] is True
                assert shutdown.result(timeout=5)["ok"] is True
            assert process.wait(timeout=5) == 0
            assert (run_dir / "helper-stopped").read_bytes() == b"stopped\n"
            assert not sock.exists()
        finally:
            release.touch()
            if process.poll() is None:
                process.kill()
            process.wait(timeout=5)
