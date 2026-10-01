"""Operation custody survives clock correction, refuses unknown owners and settles stranded work."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import subprocess
import sys
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import psutil
import pytest

from base.native_process import ownership
from base.native_process.ownership import OwnedProcess
from services.backup_scheduler.operation import custody
from services.backup_scheduler.operation.custody import NativeProcess
from services.backup_scheduler.tests.operation_support import (
    control_dir,
    exited_worker,
    operation_kind,
    release_held,
    stub_worker,
    until,
)

__all__ = ["release_held"]  # the autouse per-test release of unresolved leaders


def _current() -> NativeProcess:
    return NativeProcess.capture(psutil.Process())


def test_linux_clock_correction_preserves_the_receipt(monkeypatch: pytest.MonkeyPatch) -> None:
    """A wall-clock step moves the birth time but never the process identity."""
    current = _current()
    before = replace(current, process=replace(current.process, starttime=123))
    after = replace(before, process=replace(before.process, birth=before.process.birth + 3600))
    monkeypatch.setattr(ownership, "sys", SimpleNamespace(platform="linux"))
    captures = iter([after.process])

    def capture(_cls: type[OwnedProcess], _process: psutil.Process) -> OwnedProcess:
        return next(captures)

    monkeypatch.setattr(OwnedProcess, "capture", classmethod(capture))
    original = before.value()
    assert before.present() is not None
    assert before.same_birth(after)
    assert before != after
    assert before.value() == original


@pytest.mark.parametrize("change", ["tick", "pid", "boot"])
def test_independent_native_identity_rejects_replacement(change: str) -> None:
    current = _current()
    before = replace(current, process=replace(current.process, starttime=123))
    after = replace(before, process=replace(before.process, birth=before.process.birth + 1))
    if change == "tick":
        after = replace(after, process=replace(after.process, starttime=124))
    elif change == "pid":
        after = replace(after, process=replace(after.process, pid=after.process.pid + 1))
    else:
        after = replace(after, boot_id="another-boot")
    assert not before.same_birth(after)


def test_linux_receipt_without_start_ticks_is_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    current = _current()
    value = replace(current, process=replace(current.process, starttime=None)).value()
    monkeypatch.setattr(ownership, "sys", SimpleNamespace(platform="linux"))
    with pytest.raises(RuntimeError, match="ticks"):
        NativeProcess.from_value(value)


def _dead_child() -> tuple[subprocess.Popen[str], int, float, int]:
    """A real child that has exited but is not yet reaped (a zombie).

    The caller must popen.wait() in a finally to reap it."""
    process = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    probe = psutil.Process(process.pid)
    created_at = probe.create_time()
    pgid = os.getpgid(process.pid)
    # SIGKILL: SIGTERM is ignored when this suite runs inside a shell session
    # (SIG_IGN is inherited), and the zombie post-condition needs a real death.
    process.kill()
    deadline = time.monotonic() + 10
    while probe.status() != psutil.STATUS_ZOMBIE and time.monotonic() < deadline:
        time.sleep(0.01)
    assert probe.status() == psutil.STATUS_ZOMBIE
    return process, process.pid, created_at, pgid


def test_present_sees_an_unreaped_zombie_but_live_does_not() -> None:
    """A stopped-but-unreaped worker still pins its PID and group number, so
    closure proofs count it as present; it runs nothing, so it is not live."""
    process, pid, _created_at, _pgid = _dead_child()
    try:
        native = NativeProcess.capture(psutil.Process(pid))
        assert native.present() is not None
        assert native.live() is None
    finally:
        process.wait(timeout=10)


# ── Operation custody: quarantine, blocking, retirement ──────────────────────


def test_admission_finishes_a_proven_closure_and_retires_commits(tmp_path: Path) -> None:
    """A controller that died after its closure proof or commit blocks nothing;
    the unfinished quarantine still runs the kind's sanitizer."""
    sanitized: list[Path] = []

    def sanitize(work: Path, _worker: custody.OperationWorker | None) -> None:
        sanitized.append(work)
        (work / "artifact.dump.partial").unlink()

    closed = control_dir(tmp_path, "closed", closure={"proven_by": "controller"})
    (closed / "artifact.dump.partial").write_bytes(b"PLAINTEXT")
    committed = control_dir(tmp_path, "done", closure={}, committed={})
    retired = tmp_path / "controls" / ".retired-torn"
    retired.mkdir()
    custody.admit(operation_kind(tmp_path, sanitize=sanitize))
    assert sorted(path.name for path in (tmp_path / "controls").iterdir()) == []
    (entry,) = custody.quarantine_entries(tmp_path / "quarantine")
    assert sanitized == [closed] and not list(entry.rglob("*.partial"))
    assert "proving closure" in (entry / "failure.txt").read_text()
    assert not committed.exists() and not retired.exists()


def test_unproven_custody_blocks_until_retire_reproves_closure(tmp_path: Path) -> None:
    work = control_dir(tmp_path, "dead-controller", worker=exited_worker())
    kind = operation_kind(tmp_path)
    with pytest.raises(custody.OperationBlockedError, match="before proving closure"):
        custody.admit(kind)
    (preview,) = custody.retire_blocked(kind, confirm=False)
    assert preview.proven and preview.entry is None and work.is_dir()
    assert "is empty" in preview.reason or "later process" in preview.reason
    (report,) = custody.retire_blocked(kind, confirm=True)
    assert report.entry is not None and not work.exists()
    assert json.loads((report.entry / "closure.json").read_text())["proven_by"] == "retire"
    custody.admit(kind)  # released


def test_retire_refuses_while_the_worker_is_present(tmp_path: Path) -> None:
    process = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True
    )
    try:
        native = NativeProcess.capture(psutil.Process(process.pid))
        work = control_dir(tmp_path, "live", worker={"pid": process.pid, "native": native.value()})
        (report,) = custody.retire_blocked(operation_kind(tmp_path), confirm=True)
        assert not report.proven and "still present" in report.reason and work.is_dir()
        unresolved = control_dir(tmp_path, "later", unresolved={}, closure={})
        reasons = dict(custody.blocked_operations(operation_kind(tmp_path)))
        assert "confirmed it later" in reasons[unresolved]
    finally:
        process.kill()
        process.wait(timeout=10)


def test_retire_accepts_a_reboot_and_refuses_an_unrecorded_launch(tmp_path: Path) -> None:
    rebooted = control_dir(tmp_path, "rebooted")
    (rebooted / "operation.json").write_text(json.dumps({"boot_id": "an-earlier-boot"}))
    launching = control_dir(tmp_path, "launching")
    reports = {r.work: r for r in custody.retire_blocked(operation_kind(tmp_path), confirm=False)}
    assert reports[rebooted].proven and "rebooted" in reports[rebooted].reason
    assert not reports[launching].proven and "reboot" in reports[launching].reason


def test_quarantine_keeps_the_newest_entry_within_count_and_byte_bounds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(custody, "QUARANTINE_KEEP", 2)
    monkeypatch.setattr(custody, "QUARANTINE_MAX_BYTES", 100)
    root = tmp_path / "quarantine"
    for index in range(3):
        (root / f"2026010{index}T000000Z-test-{index}").mkdir(parents=True)
    kind = operation_kind(tmp_path)
    big = control_dir(tmp_path, "big", closure={})
    (big / "stderr.log").write_bytes(b"x" * 500)
    entry = custody.quarantine(kind, big, "failure")
    assert custody.quarantine_entries(root) == [entry]  # over both bounds, newest kept


def test_logical_kinds_have_separate_control_roots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A blocked weekly restore drill never stops the daily dump."""
    from services.backup_scheduler import worker as logical

    monkeypatch.setattr(logical, "ava_home", lambda: tmp_path)
    drill = logical.restore_drill_kind()
    (drill.control_root / ".operation-stuck").mkdir(parents=True)
    with pytest.raises(custody.OperationBlockedError):
        custody.admit(drill)
    dump = logical.dump_kind()
    dump.control_root.mkdir(parents=True)
    custody.admit(dump)
    assert dump.control_root != drill.control_root


async def test_launch_failure_quarantines_and_the_next_run_proceeds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from base.native_process.exec_domain import ExecProcessDomain
    from services.backup_scheduler.operation import worker_process

    def exhausted(*_args: object, **_kwargs: object) -> None:
        raise OSError(24, "Too many open files")

    monkeypatch.setattr(ExecProcessDomain, "launch_posix", exhausted)
    for _attempt in range(2):
        with pytest.raises(OSError, match="Too many open files"):
            await worker_process.run_operation("m", {}, kind=operation_kind(tmp_path), env={})
    entries = custody.quarantine_entries(tmp_path / "quarantine")
    assert len(entries) == 2 and not list((tmp_path / "controls").glob(".operation-*"))
    assert json.loads((entries[0] / "closure.json").read_text())["proven_by"] == "no-process"


async def test_worker_secrets_and_progress_never_touch_retained_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The live URL arrives on stdin; stderr progress reaches the operator live."""
    from services.backup_scheduler.operation import worker_process

    stub_worker(
        tmp_path,
        monkeypatch,
        "import hashlib\nfrom services.backup_scheduler.operation.worker_process import worker_secrets\n"
        "seen=hashlib.sha256(worker_secrets()['live_db_url'].encode()).hexdigest()\n"
        "sys.stderr.write('downloading base\\nbase extracted\\n');sys.stderr.flush()\n"
        "Path(sys.argv[2]).write_text(json.dumps({'seen':seen}))\n",
    )
    lines: list[str] = []
    secret = "postgresql://ava:s3cret@127.0.0.1:5433/ava"  # noqa: S105 -- fixture
    completed = await worker_process.run_operation(
        "m",
        {"chain": "c"},
        kind=operation_kind(tmp_path),
        env={},
        secrets={"live_db_url": secret},
        progress=lines.append,
    )
    assert completed.result == {"seen": hashlib.sha256(secret.encode()).hexdigest()}
    assert lines == ["downloading base", "base extracted"]
    retained = b"".join(path.read_bytes() for path in completed.work.rglob("*") if path.is_file())
    assert b"s3cret" not in retained
    await completed.commit(lambda: None)


async def test_custody_steps_run_off_the_event_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A slow group close never stalls the scheduler's health loop."""
    from base.native_process.exec_domain import ExecProcessDomain
    from services.backup_scheduler.operation import worker_process

    stub_worker(tmp_path, monkeypatch, "Path(sys.argv[2]).write_text('{}')\n")
    close = ExecProcessDomain.close_confirmed
    ticks, during = 0, list[int]()

    def slow_close(domain: ExecProcessDomain, deadline: float) -> None:
        started = ticks
        time.sleep(0.5)
        during.append(ticks - started)
        close(domain, deadline)

    async def tick() -> None:
        nonlocal ticks
        while True:
            ticks += 1
            await asyncio.sleep(0.02)

    monkeypatch.setattr(ExecProcessDomain, "close_confirmed", slow_close)
    ticker = asyncio.create_task(tick())
    try:
        completed = await worker_process.run_operation(
            "m", {}, kind=operation_kind(tmp_path), env={}
        )
    finally:
        ticker.cancel()
    await completed.commit(lambda: None)
    assert during and during[0] >= 5


def test_a_failing_quarantine_is_blocked_in_status_and_retire_retries_it(
    tmp_path: Path,
) -> None:
    """A proven closure whose quarantine keeps failing wedges its kind: status
    must name it and retirement must retry it, never report nothing to do."""
    failing = [True]

    def sanitize(_work: Path, _worker: custody.OperationWorker | None) -> None:
        if failing[0]:
            raise RuntimeError("sanitizer refused")

    kind = operation_kind(tmp_path, sanitize=sanitize)
    work = control_dir(tmp_path, "closed", closure={"proven_by": "controller"})
    with pytest.raises(custody.OperationBlockedError, match="quarantine failed"):
        custody.admit(kind)
    ((blocked, reason),) = custody.blocked_operations(kind)
    assert blocked == work and "sanitizer refused" in reason
    (report,) = custody.retire_blocked(kind, confirm=True)
    assert report.entry is None and "sanitizer refused" in report.reason and work.is_dir()
    failing[0] = False
    (report,) = custody.retire_blocked(kind, confirm=True)
    assert report.entry is not None and not work.exists()
    custody.admit(kind)  # released


def test_retire_refusals_are_typed_and_a_later_process_proves_an_unrecorded_birth(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A PID reused after the launch record proves the unrecorded worker's group
    emptied; an older process at that PID refuses precisely; an unverifiable
    receipt refuses instead of crashing the retirement."""
    later = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        reused = control_dir(tmp_path, "reused", worker={"pid": later.pid, "native": None})
        launched = time.time() - 60
        os.utime(reused / "worker.json", (launched, launched))
        older = control_dir(tmp_path, "older", worker={"pid": os.getpid(), "native": None})
        present = control_dir(tmp_path, "present", worker=exited_worker())
        present_native = NativeProcess.from_value(
            json.loads((present / "worker.json").read_text())["native"]
        )

        def unverifiable(self: NativeProcess) -> None:
            if self == present_native:
                raise RuntimeError("cannot verify process identity")

        monkeypatch.setattr(NativeProcess, "present", unverifiable)
        reports = {
            r.work: r for r in custody.retire_blocked(operation_kind(tmp_path), confirm=False)
        }
    finally:
        later.kill()
        later.wait(timeout=10)
    assert reports[reused].proven and "born after the launch record" in reports[reused].reason
    assert reports[older].refusal is custody.Refusal.BIRTH_UNRECORDED
    assert str(os.getpid()) in reports[older].reason
    assert reports[present].refusal is custody.Refusal.UNVERIFIABLE
    assert "cannot verify" in reports[present].reason


async def test_commit_holds_the_kind_lock_against_another_controller(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A committing operation holds `closure.json` without `committed.json`;
    another controller's admission must never quarantine it as a stopped one."""
    from base.native_process.os_platform import LockTimeoutError
    from services.backup_scheduler.operation import worker_process

    stub_worker(tmp_path, monkeypatch, "Path(sys.argv[2]).write_text('{}')\n")
    completed = await worker_process.run_operation("m", {}, kind=operation_kind(tmp_path), env={})
    with pytest.raises(LockTimeoutError):
        await worker_process.run_operation("m", {}, kind=operation_kind(tmp_path), env={})
    assert completed.work.is_dir() and not custody.quarantine_entries(tmp_path / "quarantine")
    await completed.commit(lambda: None)
    assert not completed.work.exists()
    await (
        await worker_process.run_operation("m", {}, kind=operation_kind(tmp_path), env={})
    ).commit(lambda: None)


def test_each_kind_quarantines_and_prunes_only_its_own_entries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A huge stranded dump never evicts another kind's newest evidence, and
    each kind's pruning runs under that kind's own lock."""
    from services.backup_scheduler import worker as logical

    monkeypatch.setattr(logical, "ava_home", lambda: tmp_path)
    dump, drill = logical.dump_kind(), logical.restore_drill_kind()
    roots = [dump.quarantine_root, drill.quarantine_root]
    assert len(set(roots)) == 2 and all(
        r.name == k.name for r, k in zip(roots, (dump, drill), strict=True)
    )
    monkeypatch.setattr(custody, "QUARANTINE_MAX_BYTES", 100)
    evidence = custody.quarantine(drill, control_dir(tmp_path, "drill", closure={}), "failed")
    stranded = control_dir(tmp_path, "stranded", closure={})
    (stranded / "artifact.dump.enc").write_bytes(b"x" * 500)
    custody.quarantine(dump, stranded, "upload interrupted")
    assert evidence.is_dir()


async def test_a_stop_during_the_grace_keeps_the_original_failure_on_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The stop propagates, yet the quarantine records why the operation failed."""
    from services.backup_scheduler.operation import worker_process

    started = tmp_path / "started"
    stub_worker(
        tmp_path,
        monkeypatch,
        "import signal\nsignal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        f"Path({str(started)!r}).write_text('1');time.sleep(60)\n",
    )
    task = asyncio.create_task(
        worker_process.run_operation("m", {}, kind=operation_kind(tmp_path), env={}, timeout_s=0.5)
    )
    await until(started)
    await asyncio.sleep(1.0)  # the execution bound fired; the stubborn grace runs
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 30)
    (entry,) = custody.quarantine_entries(tmp_path / "quarantine")
    failure = (entry / "failure.txt").read_text()
    assert failure.startswith("TimeoutError") and "execution bound" in failure


async def test_a_broken_progress_sink_never_loses_a_passing_operation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An operator's closed stderr pipe must not fail or cancel a healthy drill."""
    from services.backup_scheduler.operation import worker_process

    stub_worker(
        tmp_path,
        monkeypatch,
        "sys.stderr.write('downloading base\\n');sys.stderr.flush();time.sleep(0.3)\n"
        "sys.stderr.write('base extracted');sys.stderr.flush()\n"
        "Path(sys.argv[2]).write_text('{}')\n",
    )

    def closed_pipe(_line: str) -> None:
        raise BrokenPipeError(32, "Broken pipe")

    completed = await worker_process.run_operation(
        "m", {}, kind=operation_kind(tmp_path), env={}, progress=closed_pipe
    )
    await completed.commit(lambda: None)
    assert not completed.work.exists() and not custody.quarantine_entries(tmp_path / "quarantine")


async def test_a_busy_kind_defers_a_scheduled_restore_drill_instead_of_failing_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An operator's retire holds the kind lock: the scheduler defers quietly,
    never raising an error-level failed-drill event or recording a success."""
    from datetime import UTC, datetime

    from base.native_process.os_platform import LockTimeoutError, file_lock
    from services.backup_scheduler import daemon
    from services.backup_scheduler.operation import worker_process

    kind = operation_kind(tmp_path)
    kind.control_root.mkdir(parents=True)
    with file_lock(kind.control_root / ".lock"), pytest.raises(custody.OperationBusyError):
        await worker_process.run_operation("m", {}, kind=kind, env={})
    assert issubclass(custody.OperationBusyError, LockTimeoutError)

    async def busy(_kind: str) -> None:
        raise custody.OperationBusyError("logical-restore-drill operations are held elsewhere")

    def not_a_success(_now: datetime) -> None:
        pytest.fail("a deferred drill is not a success")

    emitted: list[str] = []

    def emit(_channel: str, event: str, **_fields: object) -> None:
        emitted.append(event)

    monkeypatch.setattr(daemon, "load_local_dump_restore_success", lambda: None)

    def due(_now: datetime, *, last_success: datetime | None) -> bool:
        return True

    monkeypatch.setattr(daemon, "local_dump_restore_due", due)
    monkeypatch.setattr(daemon, "run_job", busy)
    monkeypatch.setattr(daemon, "record_local_dump_restore_success", not_a_success)
    monkeypatch.setattr(daemon.telemetry, "emit", emit)
    await daemon._run_due_local_dump_restore(datetime.now(UTC))
    assert emitted == []
