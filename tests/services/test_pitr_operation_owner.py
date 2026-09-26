"""The living controller owns one inherited trusted-tool operation group."""

from __future__ import annotations

import asyncio
import contextlib
import gc
import hashlib
import json
import os
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import psutil
import pytest

from services.pitr import operation_custody as custody
from services.pitr import worker_process as workers
from services.pitr.operation_custody import OperationKind
from shared.exec_process_domain import ExecProcessDomain
from shared.native_process.ownership import OwnedProcess

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="PITR is POSIX-only")


@pytest.fixture(autouse=True)
def _release_held() -> Iterator[None]:
    """Unresolved leaders are process-lifetime state; close them per test."""
    yield
    with custody._HELD_LOCK:
        held = list(custody._HELD)
        custody._HELD.clear()
    for item in held:
        if item.process.returncode is None:
            with contextlib.suppress(Exception):
                os.killpg(item.process.pid, signal.SIGKILL)
            item.process.wait(timeout=5)


def _kind(tmp_path: Path, **changes: Any) -> OperationKind:
    return OperationKind("test", tmp_path / "controls", tmp_path / "quarantine", **changes)


def _entries(tmp_path: Path, kind: str | None = None) -> list[Path]:
    """Quarantined operations of the test kind, or of one production kind."""
    root = tmp_path / "quarantine"
    return custody.quarantine_entries(root if kind is None else root / kind)


def _worker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, code: str):
    script = tmp_path / "worker.py"
    script.write_text("import os,sys,json,time,subprocess\nfrom pathlib import Path\n" + code)
    launch = ExecProcessDomain.launch_posix
    children: list[tuple[subprocess.Popen[bytes], ExecProcessDomain]] = []

    def spawn(argv: list[str], **kwargs: Any):
        child = launch([sys.executable, "-I", "-B", str(script), *argv[-2:]], **kwargs)
        children.append(child)
        return child

    monkeypatch.setattr(ExecProcessDomain, "launch_posix", spawn)
    return children


async def test_result_is_accepted_only_after_inherited_child_closes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pidfile = tmp_path / "descendant"
    children = _worker(
        tmp_path,
        monkeypatch,
        "p=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)'])\n"
        f"Path({str(pidfile)!r}).write_text(str(p.pid))\n"
        "Path(sys.argv[2]).write_text(json.dumps({'group':os.getpgrp()}))\n",
    )
    completed = await workers.run_operation(
        "unused", {}, kind=_kind(tmp_path), env=dict(os.environ)
    )
    pid = int(pidfile.read_text())
    assert not psutil.pid_exists(pid) or psutil.Process(pid).status() == psutil.STATUS_ZOMBIE
    assert completed.result["group"] == children[0][0].pid
    assert children[0][0].returncode == 0
    await completed.commit(lambda: None)
    assert not completed.work.exists() and not _entries(tmp_path)


@pytest.mark.parametrize("payload,code", [("{}", 3), ("invalid", 0), ("[]", 0), (None, 0)])
async def test_proven_closed_failure_quarantines_evidence_and_next_run_proceeds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, payload: str | None, code: int
) -> None:
    write = "" if payload is None else f"Path(sys.argv[2]).write_text({payload!r});"
    children = _worker(tmp_path, monkeypatch, f"{write}sys.exit({code})\n")
    with pytest.raises((RuntimeError, TypeError)) as caught:
        await workers.run_operation("unused", {}, kind=_kind(tmp_path), env=dict(os.environ))
    assert any("operation quarantined" in note for note in caught.value.__notes__)
    assert not list((tmp_path / "controls").glob(".operation-*"))
    (entry,) = _entries(tmp_path)
    assert (entry / "request.json").is_file() and (entry / "stderr.log").is_file()
    assert json.loads((entry / "closure.json").read_text())["returncode"] == code
    assert "Error" in (entry / "failure.txt").read_text()
    if payload is None:
        assert not (entry / "result.json").exists()
    else:
        assert (entry / "result.json").read_text() == payload
    assert children[0][0].returncode == code
    # Proven closure never blocks the kind: the next operation starts.
    with pytest.raises((RuntimeError, TypeError)):
        await workers.run_operation("unused", {}, kind=_kind(tmp_path), env=dict(os.environ))
    assert len(children) == 2 and len(_entries(tmp_path)) == 2


async def test_cancel_closes_group_without_touching_unrelated_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    marker = tmp_path / "started"
    children = _worker(
        tmp_path,
        monkeypatch,
        "p=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)'])\n"
        f"Path({str(marker)!r}).write_text(str(p.pid));time.sleep(60)\n",
    )
    unrelated = subprocess.Popen([sys.executable, "-I", "-B", "-c", "import time;time.sleep(60)"])
    identity = OwnedProcess.capture(psutil.Process(unrelated.pid))
    task = asyncio.create_task(
        workers.run_operation("unused", {}, kind=_kind(tmp_path), env=dict(os.environ))
    )
    try:
        deadline = time.monotonic() + 5
        while not marker.exists() and time.monotonic() < deadline:
            await asyncio.sleep(0.01)
        assert marker.exists()
        descendant = OwnedProcess.capture(psutil.Process(int(marker.read_text())))
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 5)
        assert not descendant.live()
        assert children[0][0].returncode is not None
        assert identity.live()
        # A routine stop with proven closure quarantines; it never blocks.
        (entry,) = _entries(tmp_path)
        assert (entry / "request.json").is_file()
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        identity.send_signal(signal.SIGKILL)
        unrelated.wait(timeout=5)


async def test_signal_failure_keeps_live_direct_owner_and_controls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    children = _worker(tmp_path, monkeypatch, "time.sleep(60)\n")
    killpg = os.killpg

    def deny(pgid: int, sig: int) -> None:
        assert pgid == children[0][0].pid
        raise PermissionError("private group signal refused")

    monkeypatch.setattr(os, "killpg", deny)
    try:
        with pytest.raises(custody.OperationCustodyError) as caught:
            await workers.run_operation("unused", {}, kind=_kind(tmp_path), env={}, timeout_s=0.1)
        assert isinstance(caught.value.__cause__, TimeoutError)
        assert "private group signal refused" in "".join(caught.value.__notes__)
        process, domain = children[0]
        (work,) = (tmp_path / "controls").glob(".operation-*")
        assert process.returncode is None and domain.leader_alive()
        assert (work / "unresolved.json").is_file() and (work / "request.json").is_file()
        assert custody.held_operations() == [work]
        with pytest.raises(custody.OperationBlockedError, match="operations retire"):
            await workers.run_operation("unused", {}, kind=_kind(tmp_path), env={})
        assert len(children) == 1
        (report,) = custody.retire_blocked(_kind(tmp_path), confirm=True)
        assert not report.proven and "still present" in report.reason
        # Once signals work again, the next admission's retry proves closure,
        # yet release stays an explicit operator retirement.
        monkeypatch.setattr(os, "killpg", killpg)
        with pytest.raises(custody.OperationBlockedError, match="confirmed it later"):
            await workers.run_operation("unused", {}, kind=_kind(tmp_path), env={})
        assert process.returncode is not None and custody.held_operations() == []
        (report,) = custody.retire_blocked(_kind(tmp_path), confirm=True)
        assert report.entry is not None and not work.exists()
    finally:
        monkeypatch.setattr(os, "killpg", killpg)
        for process, domain in children:
            if process.returncode is None:
                domain.close_confirmed(time.monotonic() + 5)
                process.wait(timeout=5)


async def test_unresolved_leader_stays_unreaped_after_the_error_is_dropped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A caller that logs and drops the error cannot let `Popen.__del__` reap it."""
    pids: list[int] = []
    script = tmp_path / "worker.py"
    script.write_text("import sys\nfrom pathlib import Path\nPath(sys.argv[2]).write_text('{}')\n")
    launch = ExecProcessDomain.launch_posix

    def spawn(argv: list[str], **kwargs: Any):
        process, domain = launch([sys.executable, "-I", "-B", str(script), *argv[-2:]], **kwargs)
        pids.append(process.pid)
        return process, domain

    def refuse(_domain: ExecProcessDomain, _deadline: float) -> None:
        raise TimeoutError("exec group still has live managed members")

    monkeypatch.setattr(ExecProcessDomain, "launch_posix", spawn)
    monkeypatch.setattr(ExecProcessDomain, "close_confirmed", refuse)
    with contextlib.suppress(custody.OperationCustodyError):
        await workers.run_operation("unused", {}, kind=_kind(tmp_path), env={})
    gc.collect()
    subprocess.run([sys.executable, "-c", "pass"], check=True)  # runs subprocess._cleanup
    assert psutil.Process(pids[0]).status() == psutil.STATUS_ZOMBIE


async def test_birth_capture_failure_closes_the_unowned_launch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The worker never runs its job unowned: its pinned group is closed at once."""
    from shared.exec_process_domain import ExecDomainBirthError

    marker = tmp_path / "descendant"
    _worker(
        tmp_path,
        monkeypatch,
        "p=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)'])\n"
        f"Path({str(marker)!r}).write_text(str(p.pid));time.sleep(60)\n",
    )
    capture = OwnedProcess.capture

    def deny(_cls: type[OwnedProcess], child: psutil.Process) -> OwnedProcess:
        deadline = time.monotonic() + 5
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        raise psutil.AccessDenied(child.pid)

    monkeypatch.setattr(OwnedProcess, "capture", classmethod(deny))
    with pytest.raises(ExecDomainBirthError) as caught:
        await workers.run_operation("unused", {}, kind=_kind(tmp_path), env={})
    monkeypatch.setattr(OwnedProcess, "capture", capture)
    assert isinstance(caught.value.__cause__, psutil.AccessDenied)
    assert caught.value.proc.returncode is not None
    assert _gone(_pids(marker))
    (entry,) = _entries(tmp_path)
    assert json.loads((entry / "worker.json").read_text())["native"] is None
    assert (entry / "request.json").is_file() and (entry / "closure.json").is_file()


_DUMP = "ava-20260926T000000Z.dump.enc"


def _scheduled_dump(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, digest: str, code: int
) -> tuple[Path, list[Path], list[tuple[subprocess.Popen[bytes], ExecProcessDomain]]]:
    """A fixed dump worker plus a private published directory and prune record."""
    from services import backup
    from services.backup_scheduler import worker

    children = _worker(
        tmp_path,
        monkeypatch,
        f"artifact=Path(sys.argv[2]).parent/'artifact'/{_DUMP!r}\n"
        "artifact.parent.mkdir();artifact.write_bytes(b'encrypted')\n"
        f"Path(sys.argv[2]).write_text(json.dumps(dict(artifact=artifact.name,sha256={digest!r})))\n"
        f"sys.exit({code})\n",
    )
    monkeypatch.setattr(worker, "ava_home", lambda: tmp_path)
    published = tmp_path / "published"
    published.mkdir()
    (published / "previous.partial").write_bytes(b"retain")
    monkeypatch.setattr(backup, "backup_dir", lambda: published)
    pruned: list[Path] = []

    def prune(directory: Path) -> list[Path]:
        pruned.append(directory)
        return []

    monkeypatch.setattr(backup, "_prune", prune)
    return published, pruned, children


def _close_children(children: list[tuple[subprocess.Popen[bytes], ExecProcessDomain]]) -> None:
    for process, domain in children:
        if process.returncode is None:
            domain.close_confirmed(time.monotonic() + 5)
            process.wait(timeout=5)


_ENCRYPTED_SHA256 = hashlib.sha256(b"encrypted").hexdigest()


async def test_scheduled_commit_publishes_exact_artifact_after_closure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from services.backup_scheduler import worker

    published, pruned, children = _scheduled_dump(
        tmp_path, monkeypatch, digest=_ENCRYPTED_SHA256, code=0
    )
    try:
        await worker.run_job("dump", now=datetime(2026, 9, 26, tzinfo=UTC))
        assert (published / _DUMP).read_bytes() == b"encrypted"
        assert pruned == [published]
        assert not list((tmp_path / "backups" / "operations").glob("*/.operation-*"))
        assert (published / "previous.partial").read_bytes() == b"retain"
    finally:
        _close_children(children)


@pytest.mark.parametrize("failure", ["digest", "exit", "closure", "collision"])
async def test_scheduled_commit_failure_keeps_the_staged_artifact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    """A refused commit loses no evidence: quarantined when closure is proven,
    left blocking in the controls when it is not."""
    from services.backup_scheduler import worker

    published, pruned, children = _scheduled_dump(
        tmp_path,
        monkeypatch,
        digest="0" * 64 if failure == "digest" else _ENCRYPTED_SHA256,
        code=3 if failure == "exit" else 0,
    )
    if failure == "collision":
        (published / _DUMP).write_bytes(b"prior")
    original_close = ExecProcessDomain.close_confirmed
    if failure == "closure":

        def refuse(_domain: ExecProcessDomain, _deadline: float) -> None:
            raise PermissionError("private closure refusal")

        monkeypatch.setattr(ExecProcessDomain, "close_confirmed", refuse)
    try:
        with pytest.raises(FileExistsError if failure == "collision" else RuntimeError):
            await worker.run_job("dump", now=datetime(2026, 9, 26, tzinfo=UTC))
        assert pruned == []
        prior = published / _DUMP
        assert (prior.read_bytes() == b"prior") if failure == "collision" else not prior.exists()
        holder = (
            "operations/dump/.operation-*" if failure == "closure" else "quarantine/logical-dump/*"
        )
        (staged,) = (tmp_path / "backups").glob(f"{holder}/artifact/*")
        assert staged.read_bytes() == b"encrypted"
        assert (staged.parents[1] / "request.json").is_file()
        assert (published / "previous.partial").read_bytes() == b"retain"
    finally:
        monkeypatch.setattr(ExecProcessDomain, "close_confirmed", original_close)
        _close_children(children)


async def test_unretired_controls_refuse_fresh_launch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    children = _worker(tmp_path, monkeypatch, "time.sleep(60)\n")
    controls = tmp_path / "controls"
    prior = controls / ".operation-interrupted"
    prior.mkdir(parents=True)
    request = prior / "request.json"
    request.write_bytes(b'{"original":"unknown ownership"}')
    with pytest.raises(custody.OperationBlockedError, match="stopped before proving closure"):
        await workers.run_operation("unused", {}, kind=_kind(tmp_path), env={})
    assert children == []
    assert request.read_bytes() == b'{"original":"unknown ownership"}'


def _pids(path: Path) -> list[int]:
    return [int(line) for line in path.read_text().split()] if path.exists() else []


async def _until(path: Path) -> None:
    deadline = time.monotonic() + 10
    while not path.exists() and time.monotonic() < deadline:
        await asyncio.sleep(0.01)
    assert path.exists(), f"{path.name} was never written"


def _gone(pids: list[int]) -> bool:
    return not any(
        OwnedProcess.capture(psutil.Process(pid)).live() for pid in pids if psutil.pid_exists(pid)
    )


async def test_late_forks_after_worker_exit_close_before_acceptance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A descendant still forking after its worker exited cannot outlive acceptance."""
    log = tmp_path / "forked"
    forker = (
        "import subprocess,sys,time\n"
        "while True:\n"
        " p=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)'])\n"
        f" open({str(log)!r},'a').write(f'{{p.pid}}\\n')\n"
    )
    children = _worker(
        tmp_path,
        monkeypatch,
        f"p=subprocess.Popen([sys.executable,'-c',{forker!r}])\n"
        f"open({str(log)!r},'a').write(f'{{p.pid}}\\n')\n"
        f"while len(Path({str(log)!r}).read_text().split())<4: time.sleep(0.01)\n"
        "Path(sys.argv[2]).write_text(json.dumps({'late':True}))\n",
    )
    completed = await asyncio.wait_for(
        workers.run_operation("unused", {}, kind=_kind(tmp_path), env={}), 30
    )
    group = children[0][0].pid
    try:
        assert completed.result == {"late": True}
        assert children[0][0].returncode == 0
        assert len(_pids(log)) >= 4 and _gone(_pids(log))
        # A child forked while its parent was being killed stays in the group
        # even when its PID never reached the log.
        assert _group_members(group) == []
    finally:
        _kill_group(group)
        await completed.commit(lambda: None)


def _group_members(group: int) -> list[int]:
    members: list[int] = []
    for process in psutil.process_iter(["pid", "status"]):
        with contextlib.suppress(ProcessLookupError, psutil.Error):
            if process.info["status"] != psutil.STATUS_ZOMBIE and os.getpgid(process.pid) == group:
                members.append(process.pid)
    return members


def _kill_group(group: int) -> None:
    """Test cleanup: any escaped member still carries the reaped leader's group."""
    for pid in _group_members(group):
        with contextlib.suppress(ProcessLookupError, psutil.Error):
            OwnedProcess.capture(psutil.Process(pid)).send_signal(signal.SIGKILL)


async def test_descendant_holding_worker_output_cannot_delay_acceptance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Worker output is file-backed: no controller read waits for a late writer."""
    marker = tmp_path / "writer"
    writer = "import sys,time\nwhile True:\n sys.stderr.write('late\\n');sys.stderr.flush();time.sleep(.01)\n"
    _worker(
        tmp_path,
        monkeypatch,
        f"p=subprocess.Popen([sys.executable,'-c',{writer!r}])\n"
        f"Path({str(marker)!r}).write_text(str(p.pid))\n"
        "log=Path(sys.argv[2]).parent/'stderr.log'\n"
        "while b'late' not in log.read_bytes(): time.sleep(0.01)\n"
        "Path(sys.argv[2]).write_text('{}')\n",
    )
    # An EOF wait on the inherited output would never finish while the writer lives.
    completed = await asyncio.wait_for(
        workers.run_operation("unused", {}, kind=_kind(tmp_path), env={}), 10
    )
    try:
        assert _gone(_pids(marker))
        assert b"late" in (completed.work / "stderr.log").read_bytes()
    finally:
        await completed.commit(lambda: None)


async def test_worker_capture_failure_after_birth_closes_group_and_quarantines(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    marker = tmp_path / "descendant"
    children = _worker(
        tmp_path,
        monkeypatch,
        "p=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)'])\n"
        f"Path({str(marker)!r}).write_text(str(p.pid));time.sleep(60)\n",
    )

    def deny(_cls: type[custody.NativeProcess], process: psutil.Process) -> None:
        deadline = time.monotonic() + 5
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        raise psutil.AccessDenied(process.pid)

    monkeypatch.setattr(custody.NativeProcess, "capture", classmethod(deny))
    with pytest.raises(psutil.AccessDenied) as caught:
        await workers.run_operation("unused", {}, kind=_kind(tmp_path), env={})
    assert any("operation quarantined" in note for note in caught.value.__notes__)
    assert children[0][0].returncode is not None
    assert _gone(_pids(marker))
    (entry,) = _entries(tmp_path)
    assert (entry / "request.json").is_file()


_COOPERATIVE = (
    "import signal\n"
    "from services.pitr.worker_process import worker_request\n"
    "request, output = worker_request(sys.argv)\n"
    "{ignore}"
    "try:\n"
    " Path(request['started']).write_text('started');time.sleep(60)\n"
    "finally:\n"
    " Path(request['cleaned']).write_text('cleaned')\n"
)


@pytest.mark.parametrize("stubborn", [False, True])
async def test_cancel_lets_worker_unwind_private_cleanup_before_group_close(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stubborn: bool
) -> None:
    started, cleaned = tmp_path / "started", tmp_path / "cleaned"
    ignore = "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n" if stubborn else ""
    children = _worker(tmp_path, monkeypatch, _COOPERATIVE.format(ignore=ignore))
    request = {"started": str(started), "cleaned": str(cleaned)}
    task = asyncio.create_task(
        workers.run_operation("unused", request, kind=_kind(tmp_path), env={})
    )
    await _until(started)
    cancelled = time.monotonic()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 10)
    elapsed = time.monotonic() - cancelled
    assert children[0][0].returncode is not None
    # Cooperative cleanup finishes inside the grace; a stubborn worker is killed after it.
    assert cleaned.exists() is not stubborn
    assert (elapsed >= custody.TERMINATE_GRACE_S) is stubborn
    (entry,) = _entries(tmp_path)
    assert (entry / "request.json").is_file()


async def test_repeated_cancel_during_grace_still_closes_and_stays_cancelled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    started, cleaned = tmp_path / "started", tmp_path / "cleaned"
    ignore = "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
    children = _worker(tmp_path, monkeypatch, _COOPERATIVE.format(ignore=ignore))
    request = {"started": str(started), "cleaned": str(cleaned)}
    stop = threading.Event()
    task = asyncio.create_task(
        workers.run_operation("unused", request, kind=_kind(tmp_path), env={}, stop=stop)
    )
    await _until(started)  # the worker ignores SIGTERM from here on
    stop.set()
    await asyncio.sleep(0.5)  # the stop was observed; the grace is running
    cancelled = time.monotonic()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 10)
    # The second cancel cuts the grace short but never skips confirmed closure.
    assert time.monotonic() - cancelled < custody.TERMINATE_GRACE_S
    assert children[0][0].returncode is not None and not cleaned.exists()
    (entry,) = _entries(tmp_path)
    assert (entry / "request.json").is_file()


async def test_worker_inherits_no_controller_descriptors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A fresh exec worker cannot hold a scheduler pipe or tracker descriptor."""
    read, write = os.pipe()
    os.set_inheritable(read, True)
    os.set_inheritable(write, True)
    listing = "/proc/self/fd" if sys.platform == "linux" else "/dev/fd"
    _worker(
        tmp_path,
        monkeypatch,
        f"fds=sorted(int(fd) for fd in os.listdir({listing!r}))\n"
        "Path(sys.argv[2]).write_text(json.dumps({'fds':fds}))\n",
    )
    try:
        completed = await workers.run_operation("unused", {}, kind=_kind(tmp_path), env={})
    finally:
        os.close(read)
        os.close(write)
    fds = cast(list[int], completed.result["fds"])
    assert read not in fds and write not in fds
    assert set(fds) <= {0, 1, 2, 3}  # stdio plus the listing's own directory handle
    await completed.commit(lambda: None)


async def test_spawned_python_child_inherits_the_operation_group(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A multiprocessing spawn child (the base stream producer's shape) is a member."""
    record = tmp_path / "spawned"
    children = _worker(
        tmp_path,
        monkeypatch,
        "import multiprocessing\n"
        "if __name__=='__main__':\n"
        " p=multiprocessing.get_context('spawn').Process(target=time.sleep,args=(60,))\n"
        " p.start()\n"
        f" Path({str(record)!r}).write_text(f'{{p.pid}} {{os.getpgid(p.pid)}}')\n"
        " Path(sys.argv[2]).write_text('{}');sys.stdout.flush();os._exit(0)\n",
    )
    completed = await workers.run_operation("unused", {}, kind=_kind(tmp_path), env={})
    pid, group = _pids(record)
    assert group == children[0][0].pid and _gone([pid])
    await completed.commit(lambda: None)


def _result_worker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, result: object) -> None:
    _worker(tmp_path, monkeypatch, f"Path(sys.argv[2]).write_text({json.dumps(result)!r})\n")


def _operation_dirs(root: Path) -> list[Path]:
    return sorted(root.glob(".operation-*"))


async def test_deferred_base_candidate_retires_clean_controls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from services.pitr import base_worker
    from shared.platform import LockTimeoutError

    monkeypatch.setattr(base_worker, "ava_home", lambda: tmp_path)
    _result_worker(tmp_path, monkeypatch, {"deferred": "backup_lock", "detail": "busy"})
    with pytest.raises(LockTimeoutError):
        await base_worker.run_candidate()
    assert not _operation_dirs(tmp_path / "physical-backup" / "base-control")
    assert not _entries(tmp_path / "physical-backup", "base-candidate")


async def test_space_deferral_is_clean_and_never_quarantined(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from services.pitr import base_operation_runtime

    detail = "restore proof deferred: requires 9 bytes but only 1 are free"
    _result_worker(tmp_path, monkeypatch, {"deferred": "space", "detail": detail})
    with pytest.raises(custody.OperationDeferred) as caught:
        await base_operation_runtime.run_restore_input(_restore_inputs(tmp_path))
    assert (caught.value.reason, caught.value.detail) == ("space", detail)
    assert not _operation_dirs(tmp_path / "restore-control")
    assert not _entries(tmp_path, "restore-proof")


async def test_base_commit_failure_after_closure_quarantines_candidate_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A closed zero-exit worker whose commit refuses keeps every control file."""
    from services.pitr import base_worker
    from tests.services.test_pitr_base_scheduler import _candidate

    monkeypatch.setattr(base_worker, "ava_home", lambda: tmp_path)
    candidate = _candidate("20260926T000000Z")
    _result_worker(tmp_path, monkeypatch, {"candidate_json": candidate.to_json()})
    root = tmp_path / "physical-backup"
    owner = root / "base-facts" / f"{candidate.chain_id}.owner.json"
    owner.parent.mkdir(parents=True)
    owner.write_text(json.dumps({"chain_id": candidate.chain_id, "native": _other_worker()}))
    with pytest.raises(RuntimeError, match="another operation worker"):
        await base_worker.run_candidate()
    assert not _operation_dirs(root / "base-control")
    (entry,) = _entries(root, "base-candidate")
    assert json.loads((entry / "result.json").read_text())["candidate_json"]
    # Another worker's receipt is never claimed by this operation's quarantine.
    assert owner.is_file() and not (root / "base-manifests").exists()


def _other_worker() -> dict[str, object]:
    current = custody.NativeProcess.capture(psutil.Process())
    return replace(current, process=replace(current.process, pid=1)).value()


def _restore_inputs(tmp_path: Path):
    from services.pitr.base_operation_runtime import RestoreWorkerInput
    from services.pitr.restore_proof import RestoreSpaceBudget
    from tests.services.test_pitr_base_scheduler import _candidate

    return RestoreWorkerInput(
        _candidate("20260926T000000Z").to_json(),
        tmp_path,
        tmp_path / "ack",
        tmp_path / "backup.key",
        "gcs",
        (),
        RestoreSpaceBudget(0, 0, 0),
        "postgresql://viewer@127.0.0.1:1/ava",
        "/live/data",
        Path("/unused/pg_ctl"),
        Path("/unused/pg_verifybackup"),
    )


async def test_restore_retirement_failure_after_closure_quarantines_controls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from services.pitr import base_operation_runtime

    outcome = {"chain_id": "20260926T000000Z", "candidate_sha256": "c", "pending_sha256": "p"}
    _result_worker(tmp_path, monkeypatch, outcome)
    with pytest.raises(FileNotFoundError):  # the pending proof it names does not exist
        await base_operation_runtime.run_restore_input(_restore_inputs(tmp_path))
    assert not _operation_dirs(tmp_path / "restore-control")
    (entry,) = _entries(tmp_path, "restore-proof")
    assert json.loads((entry / "result.json").read_text()) == outcome
    assert "live_db_url" not in json.loads((entry / "request.json").read_text())


@pytest.mark.parametrize("outcome", ["pass", "fail"])
async def test_drill_accepts_only_passing_evidence_for_the_candidate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, outcome: str
) -> None:
    from services.pitr import base_operation_runtime

    scratch = tmp_path / "scratch"
    evidence = {"outcome": outcome, "chain_id": "20260926T000000Z"}
    _worker(
        tmp_path,
        monkeypatch,
        "import hashlib\n"
        "drill=json.loads(Path(sys.argv[1]).read_text())['drill']\n"
        "scratch=Path(drill['scratch']);scratch.mkdir()\n"
        f"payload={json.dumps(evidence)!r}.encode()\n"
        "(scratch/'drill-evidence.json').write_bytes(payload)\n"
        "digest=hashlib.sha256(payload).hexdigest()\n"
        "Path(sys.argv[2]).write_text(json.dumps({'evidence_sha256':digest}))\n",
    )
    run = base_operation_runtime.run_drill_input(
        _restore_inputs(tmp_path),
        scratch=scratch,
        target_lsn="0/180",
        target_wall="2026-09-26T00:00:00+00:00",
        timeout_seconds=60,
    )
    if outcome == "pass":
        assert await run == evidence
        assert not _entries(tmp_path, "pitr-drill")
    else:
        with pytest.raises(RuntimeError, match="did not complete"):
            await run
        assert len(_entries(tmp_path, "pitr-drill")) == 1  # the outcome is data, not a block
    assert not _operation_dirs(tmp_path / "drill-control")
    assert json.loads((scratch / "drill-evidence.json").read_text()) == evidence


async def test_scheduled_commit_runs_off_the_event_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Hashing and linking a large artifact must not stall the health loop."""
    from services.backup_scheduler import worker

    name = "ava-20260926T000000Z.dump.enc"
    _worker(
        tmp_path,
        monkeypatch,
        f"artifact=Path(sys.argv[2]).parent/'artifact'/{name!r}\n"
        "artifact.parent.mkdir();artifact.write_bytes(b'encrypted')\n"
        "Path(sys.argv[2]).write_text(json.dumps(dict(artifact=artifact.name,sha256='d')))\n",
    )
    monkeypatch.setattr(worker, "ava_home", lambda: tmp_path)
    ticks = 0
    during: list[int] = []

    def slow_commit(staged: Path, digest: str) -> Path:
        started = ticks
        time.sleep(0.5)
        during.append(ticks - started)
        return staged

    monkeypatch.setattr(worker, "commit_scheduled_backup", slow_commit)

    async def tick() -> None:
        nonlocal ticks
        while True:
            ticks += 1
            await asyncio.sleep(0.02)

    ticker = asyncio.create_task(tick())
    try:
        await worker.run_job("dump", now=datetime(2026, 9, 26, tzinfo=UTC))
    finally:
        ticker.cancel()
    assert during and during[0] >= 5  # the loop kept running while the commit blocked
