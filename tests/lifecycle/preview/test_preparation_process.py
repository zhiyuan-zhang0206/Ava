"""Finite preparation closes nested groups, not merely its coordinator's group."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

import psutil
import pytest

from scripts.preview import preparation_process as native


def test_native_capability_failure_precedes_preparation_effects(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unavailable() -> None:
        raise OSError("pidfd unavailable")

    def spawned(*_args: object, **_kwargs: object) -> None:
        pytest.fail("cannot create a child without native process custody")

    monkeypatch.setattr(native.sys, "platform", "linux")
    monkeypatch.setattr(native.pidfd, "require_available", unavailable)
    monkeypatch.setattr(native.subprocess, "Popen", spawned)
    with pytest.raises(OSError, match="pidfd unavailable"):
        native.run(
            ["no-spawn"],
            cwd=tmp_path,
            env={},
            log=tmp_path / "log",
            evidence=tmp_path / "custody.json",
            timeout=1,
        )
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("changed", [False, True])
def test_signal_is_bound_to_pidfd_and_rechecks_session_identity(
    monkeypatch: pytest.MonkeyPatch, *, changed: bool
) -> None:
    member = native.Member(42, 1, 42, 40, 123, "S")
    current = native.Member(42, 1, 42, 99, 999, "S") if changed else member
    signals: list[tuple[int, int]] = []
    closed: list[int] = []

    def opened(_pid: int) -> int:
        return 70

    def sent(fd: int, sig: int) -> None:
        signals.append((fd, sig))

    def observed(_pid: int) -> native.Member:
        return current

    def live(_fd: int) -> bool:
        return False

    monkeypatch.setattr(native.pidfd, "open_process", opened)
    monkeypatch.setattr(native.pidfd, "send_signal", sent)
    monkeypatch.setattr(native.sys, "platform", "linux")
    monkeypatch.setattr(native.os, "close", closed.append)
    monkeypatch.setattr(native, "_member", observed)
    monkeypatch.setattr(native, "_ended", live)
    if changed:
        with pytest.raises(RuntimeError, match="changed before native signal"):
            native._signal(member)
        assert not signals
    else:
        native._signal(member)
        assert signals == [(70, signal.SIGKILL)]
    assert closed == [70]


def test_unknown_session_closure_retains_leader_and_original_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Process:
        pid = 41
        args = ("finite-test",)

        def wait(self) -> int:
            pytest.fail("uncertain session must not reap its pinned leader")

    process = cast(subprocess.Popen[bytes], Process())

    def timeout(*_args: object) -> None:
        raise subprocess.TimeoutExpired("finite-test", 1)

    def unknown(_session: int) -> list[native.Member]:
        raise PermissionError("native membership unavailable")

    record: dict[str, Any] = {}
    monkeypatch.setattr(native, "_wait", timeout)
    monkeypatch.setattr(native, "_members", unknown)
    try:
        with pytest.raises(RuntimeError, match=r"TimeoutExpired.*custody unresolved"):
            native._finish(process, 5, 1, record, lambda: None)
        assert record["result"] == "failed" and record["custody"] == "unresolved"
        assert process in native._UNRESOLVED
    finally:
        native._UNRESOLVED.remove(process)


def _alive(pid: int, birth: float) -> bool:
    try:
        process = psutil.Process(pid)
        return process.create_time() == birth and process.status() != psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return False


@pytest.mark.parametrize("ambiguous", [False, True])
def test_failed_spawn_distinguishes_known_absence_from_ambiguous_admission(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, ambiguous: bool
) -> None:
    error = KeyboardInterrupt if ambiguous else FileNotFoundError

    def refused(*_args: object, **_kwargs: object) -> None:
        raise error("preparation interpreter")

    monkeypatch.setattr(native.sys, "platform", "linux")
    monkeypatch.setattr(native.pidfd, "require_available", lambda: None)
    monkeypatch.setattr(native.subprocess, "Popen", refused)
    evidence = tmp_path / "custody.json"
    with pytest.raises(error, match="preparation interpreter"):
        native.run(
            ["absent"], cwd=tmp_path, env={}, log=tmp_path / "log", evidence=evidence, timeout=1
        )
    record = json.loads(evidence.read_text())
    assert record["result"] == "failed"
    assert record["custody"] == ("unresolved" if ambiguous else "not-started")
    assert "session" not in record


@pytest.mark.parametrize("signum", [signal.SIGINT, signal.SIGTERM])
def test_deferred_real_cancellation_restores_handlers(signum: int) -> None:
    previous = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}
    mask = signal.pthread_sigmask(signal.SIG_BLOCK, [])
    with (
        pytest.raises(KeyboardInterrupt, match="preparation cancelled"),
        native._defer_cancellation() as cancelled,
    ):
        os.kill(os.getpid(), signum)
        assert signal.pthread_sigmask(signal.SIG_BLOCK, []) == mask
        cancelled()
    assert {sig: signal.getsignal(sig) for sig in previous} == previous


def _cancelling_spawn(
    monkeypatch: pytest.MonkeyPatch,
    mask_path: Path,
    children: list[subprocess.Popen[bytes]],
    boundary: str,
    signum: int,
) -> None:
    original = subprocess.Popen

    def cancel(process: subprocess.Popen[bytes]) -> None:
        children.append(process)
        deadline = time.monotonic() + 5
        while not mask_path.exists():
            if time.monotonic() >= deadline:
                raise RuntimeError("spawn fixture did not publish its inherited mask")
            time.sleep(0.01)
        os.kill(os.getpid(), signum)

    if boundary == "inside-popen":
        execute = cast(Any, original)._execute_child

        def injected(process: subprocess.Popen[bytes], *args: Any, **kwargs: Any) -> None:
            execute(process, *args, **kwargs)
            cancel(process)

        monkeypatch.setattr(original, "_execute_child", injected)
    else:

        def returned(*args: Any, **kwargs: Any) -> subprocess.Popen[bytes]:
            process = cast("subprocess.Popen[bytes]", original(*args, **kwargs))
            cancel(process)
            return process

        monkeypatch.setattr(native.subprocess, "Popen", returned)


def _cancellation_child(root: Path) -> tuple[Path, Path]:
    mask_path, child = root / "mask.json", root / "child.py"
    child.write_text(
        "import json,pathlib,signal,time\n"
        f"pathlib.Path({str(mask_path)!r}).write_text(json.dumps([int(sig) for sig in signal.pthread_sigmask(signal.SIG_BLOCK,[])]))\n"
        "time.sleep(30)\n"
    )
    return mask_path, child


@pytest.mark.parametrize("boundary", ["inside-popen", "before-binding"])
@pytest.mark.parametrize("signum", [signal.SIGINT, signal.SIGTERM])
def test_real_spawn_boundary_defers_signal_without_masking_exec_child(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, boundary: str, signum: int
) -> None:
    mask_path, child = _cancellation_child(tmp_path.resolve())
    mask = {int(sig) for sig in signal.pthread_sigmask(signal.SIG_BLOCK, [])}
    children: list[subprocess.Popen[bytes]] = []
    _cancelling_spawn(monkeypatch, mask_path, children, boundary, signum)
    admitted = False
    try:
        with (
            pytest.raises(KeyboardInterrupt),
            native._defer_cancellation() as cancelled,
        ):
            process = subprocess.Popen(  # noqa: S603 -- exact test-owned child script.
                [sys.executable, "-I", str(child)]
            )
            admitted = True
            assert process is children[0] and process.poll() is None
            assert set(json.loads(mask_path.read_text())) == mask
            cancelled()
        assert admitted
    finally:
        for process in children:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=5)


@pytest.mark.skipif(sys.platform != "linux", reason="native Linux pidfd/session admission")
@pytest.mark.parametrize("boundary", ["inside-popen", "before-binding"])
@pytest.mark.parametrize("signum", [signal.SIGINT, signal.SIGTERM])
def test_native_signal_during_spawn_is_delivered_only_after_custody_admission(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, boundary: str, signum: int
) -> None:
    root = tmp_path.resolve()
    mask_path, child = _cancellation_child(root)
    evidence = root / "custody.json"
    mask = {int(sig) for sig in signal.pthread_sigmask(signal.SIG_BLOCK, [])}
    previous = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}
    children: list[subprocess.Popen[bytes]] = []
    _cancelling_spawn(monkeypatch, mask_path, children, boundary, signum)
    try:
        with pytest.raises(KeyboardInterrupt, match="preparation cancelled"):
            native.run(
                [sys.executable, "-I", "-B", str(child)],
                cwd=root,
                env={"PATH": os.defpath},
                log=root / "log",
                evidence=evidence,
                timeout=5,
            )
        record = json.loads(evidence.read_text())
        assert record["custody"] == "closed" and record["result"] == "failed"
        assert record["session"] == record["leader"]["pid"] == children[0].pid
        assert record["leader"]["session"] == children[0].pid
        assert children[0].returncode is not None
        assert not native._members(children[0].pid)
        assert set(json.loads(mask_path.read_text())) == mask
        assert {sig: signal.getsignal(sig) for sig in previous} == previous
    finally:
        for process in children:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=5)


def _scripts(root: Path, mode: str) -> tuple[Path, Path]:
    receipt = root / "child.json"
    delay = 0.2 if mode == "success" else 30
    leaf = root / "leaf.py"
    leaf.write_text(
        "import json,os,pathlib,subprocess,sys,psutil\n"
        f"child=subprocess.Popen([sys.executable,'-I','-c','import time; time.sleep({delay})'])\n"
        f"pathlib.Path({str(receipt)!r}).write_text(json.dumps([child.pid,psutil.Process(child.pid).create_time(),os.getsid(child.pid),os.getpgid(child.pid)]))\n"
    )
    coordinator = root / "coordinator.py"
    coordinator.write_text(
        "import os,sys\nfrom pathlib import Path\n"
        f"sys.path.insert(0,{str(Path(__file__).resolve().parents[3])!r})\n"
        "from shared.posix_command import run_owned_command\n"
        f"run_owned_command([sys.executable,'-I','-B',{str(leaf)!r}],cwd=Path({str(root)!r}),env={{'PATH':os.defpath}},timeout=20)\n"
    )
    return receipt, coordinator


@pytest.mark.skipif(sys.platform != "linux", reason="native Linux pidfd/session boundary")
@pytest.mark.parametrize("mode", ["success", "timeout", "coordinator-killed", "interrupted"])
def test_native_nested_preparation_session_closure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    if sys.platform != "linux":
        pytest.skip("native Linux pidfd/session boundary")
    root = tmp_path.resolve()
    receipt, coordinator = _scripts(root, mode)
    original_wait = native._wait

    def wait(
        process: subprocess.Popen[bytes], fd: int, timeout: float, cancelled: Callable[[], None]
    ) -> None:
        if sys.platform != "linux":
            raise RuntimeError("native Linux fixture requires pidfds")
        if mode in {"coordinator-killed", "interrupted"}:
            deadline = time.monotonic() + 5
            while not receipt.exists():
                if time.monotonic() >= deadline:
                    raise RuntimeError("nested fixture did not start")
                time.sleep(0.01)
            if mode == "interrupted":
                raise KeyboardInterrupt("fixture interruption")
            native.pidfd.send_signal(fd, signal.SIGKILL)
        original_wait(process, fd, timeout, cancelled)

    monkeypatch.setattr(native, "_wait", wait)
    sibling = subprocess.Popen([sys.executable, "-I", "-c", "import time; time.sleep(30)"])
    evidence = root / "custody.json"
    try:
        kwargs: dict[str, Any] = {
            "cwd": root,
            "env": {"PATH": os.defpath},
            "log": root / "log",
            "evidence": evidence,
            "timeout": 5 if mode == "success" else 1,
        }
        if mode == "success":
            native.run([sys.executable, "-I", "-B", str(coordinator)], **kwargs)
        else:
            error = KeyboardInterrupt if mode == "interrupted" else subprocess.TimeoutExpired
            with pytest.raises(error):
                native.run([sys.executable, "-I", "-B", str(coordinator)], **kwargs)
        pid, birth, session, group = json.loads(receipt.read_text())
        observed = json.loads(evidence.read_text())
        assert session == observed["session"] and group != session
        assert observed["custody"] == "closed" and not _alive(pid, birth)
        assert observed["result"] == ("passed" if mode == "success" else "failed")
        assert sibling.poll() is None
    finally:
        if receipt.exists():
            pid, birth, *_ = json.loads(receipt.read_text())
            if _alive(pid, birth):
                # Failed negative controls close only their captured fixture task.
                fd = native.pidfd.open_process(pid)
                try:
                    if _alive(pid, birth):
                        native.pidfd.send_signal(fd, signal.SIGKILL)
                finally:
                    os.close(fd)
        sibling.kill()
        sibling.wait(timeout=5)
