"""PITR native custody survives clock correction and refuses unknown owners."""

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

from services.pitr import (
    base_candidate,
    restore_postgres,
    restore_proof,
)
from services.pitr import operation_custody as custody
from services.pitr.operation_custody import NativeProcess
from services.pitr.restore_postgres import SandboxPostgresIdentity
from services.pitr.restore_proof import LivePostgresIdentity, RestoreProofError
from shared.native_process import native_boot_id, ownership
from shared.native_process.ownership import OwnedProcess


def _current() -> NativeProcess:
    return NativeProcess.capture(psutil.Process())


def _capture_observations(
    monkeypatch: pytest.MonkeyPatch, observations: list[OwnedProcess]
) -> None:
    captures = iter(observations)

    def capture(_cls: type[OwnedProcess], _process: psutil.Process) -> OwnedProcess:
        return next(captures)

    monkeypatch.setattr(OwnedProcess, "capture", classmethod(capture))


def test_linux_clock_correction_preserves_receipt_and_live_database_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    current = _current()
    before = replace(current, process=replace(current.process, starttime=123))
    after = replace(before, process=replace(before.process, birth=before.process.birth + 3600))
    monkeypatch.setattr(ownership, "sys", SimpleNamespace(platform="linux"))
    _capture_observations(monkeypatch, [after.process])
    original = before.value()
    assert before.present() is not None
    assert before.same_birth(after)
    assert before != after
    assert before.value() == original
    first = LivePostgresIdentity(before, "/data", "system", "started", "probe")
    second = replace(first, native=after)
    assert first.unchanged(second)
    assert not first.unchanged(replace(second, system_identifier="other"))
    restore_proof._same_live(first, second)


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


def _partial(root: Path, state: str, native: NativeProcess) -> tuple[Path, Path]:
    chain = "20260926T000000Z"
    partial = root / "base-candidates" / f".{chain}.partial"
    partial.mkdir(parents=True)
    owner = root / "base-facts" / f"{chain}.owner.json"
    owner.parent.mkdir()
    owner.write_text(
        json.dumps(
            {"state": state, "native": native.value(), "pgid": os.getpgrp(), "chain_id": chain}
        )
    )
    return partial, owner


def test_expired_spawn_does_not_delete_live_creator_work(tmp_path: Path) -> None:
    partial, owner = _partial(tmp_path, "spawning", _current())
    receipt = owner.read_bytes()
    with pytest.raises(base_candidate.BaseCandidateError, match="unresolved"):
        base_candidate._recover_owned_partials(tmp_path)
    assert partial.is_dir()
    assert owner.read_bytes() == receipt


def test_dead_leader_with_unknown_group_retains_partial(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wrong = replace(_current(), process=OwnedProcess(os.getpid(), 1.0, 1))
    partial, owner = _partial(tmp_path, "running", wrong)
    receipt = owner.read_bytes()
    with pytest.raises(RuntimeError, match="unresolved"):
        base_candidate._recover_owned_partials(tmp_path)
    assert partial.is_dir()
    assert owner.read_bytes() == receipt


def test_restore_publication_refuses_changed_receipt(tmp_path: Path) -> None:
    owner = tmp_path / "owner.json"
    owner.write_bytes(b'{"generation":2}')
    with pytest.raises(RestoreProofError, match="receipt changed"):
        restore_proof._atomic_owner(owner, {"state": "stopped"}, b'{"generation":1}')
    assert owner.read_bytes() == b'{"generation":2}'


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


def test_matching_process_treats_a_zombie_as_not_live() -> None:
    """Activation #12: a stopped-but-unreaped sandbox postmaster kept passing
    the create_time probe, so cleanup refused to remove a dead restore and
    masked the real failure. A zombie runs nothing and is not live."""
    process, pid, _created_at, _pgid = _dead_child()
    try:
        assert restore_proof._matching_process(NativeProcess.capture(psutil.Process(pid))) is None
        assert not restore_proof._sandbox_is_live(
            {"sandbox_native": NativeProcess.capture(psutil.Process(pid)).value()}
        )
    finally:
        process.wait(timeout=10)


def test_matching_sandbox_treats_a_zombie_as_not_live() -> None:
    process, pid, _created_at, pgid = _dead_child()
    try:
        identity = SandboxPostgresIdentity(
            NativeProcess.capture(psutil.Process(pid)), pgid, os.getsid(0), "/data"
        )
        assert restore_postgres._matching_sandbox(identity) is None
    finally:
        process.wait(timeout=10)


# ── Operation custody: quarantine, blocking, retirement ──────────────────────


def _kind(tmp_path: Path, **changes: object) -> custody.OperationKind:
    return custody.OperationKind(
        "test",
        tmp_path / "controls",
        tmp_path / "quarantine",
        **changes,  # pyright: ignore[reportArgumentType]
    )


def _control_dir(tmp_path: Path, name: str, **records: object) -> Path:
    work = tmp_path / "controls" / f".operation-{name}"
    work.mkdir(parents=True)
    (work / "operation.json").write_text(
        json.dumps({"kind": "test", "module": "m", "boot_id": native_boot_id(), "at": "t"})
    )
    for record, value in records.items():
        (work / f"{record}.json").write_text(json.dumps(value))
    return work


def _exited_worker() -> dict[str, object]:
    """A real worker that exited and was reaped: its group is empty."""
    process = subprocess.Popen([sys.executable, "-c", "pass"], start_new_session=True)
    native = NativeProcess.capture(psutil.Process(process.pid))
    process.wait(timeout=10)
    return {"pid": process.pid, "native": native.value()}


def test_admission_finishes_a_proven_closure_and_retires_commits(tmp_path: Path) -> None:
    """A controller that died after its closure proof or commit blocks nothing;
    the unfinished quarantine still runs the kind's sanitizer."""
    sanitized: list[Path] = []

    def sanitize(work: Path, _worker: custody.OperationWorker | None) -> None:
        sanitized.append(work)
        (work / "artifact.dump.partial").unlink()

    closed = _control_dir(tmp_path, "closed", closure={"proven_by": "controller"})
    (closed / "artifact.dump.partial").write_bytes(b"PLAINTEXT")
    committed = _control_dir(tmp_path, "done", closure={}, committed={})
    retired = tmp_path / "controls" / ".retired-torn"
    retired.mkdir()
    custody.admit(_kind(tmp_path, sanitize=sanitize))
    assert sorted(path.name for path in (tmp_path / "controls").iterdir()) == []
    (entry,) = custody.quarantine_entries(tmp_path / "quarantine")
    assert sanitized == [closed] and not list(entry.rglob("*.partial"))
    assert "proving closure" in (entry / "failure.txt").read_text()
    assert not committed.exists() and not retired.exists()


def test_unproven_custody_blocks_until_retire_reproves_closure(tmp_path: Path) -> None:
    work = _control_dir(tmp_path, "dead-controller", worker=_exited_worker())
    kind = _kind(tmp_path)
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
        work = _control_dir(tmp_path, "live", worker={"pid": process.pid, "native": native.value()})
        (report,) = custody.retire_blocked(_kind(tmp_path), confirm=True)
        assert not report.proven and "still present" in report.reason and work.is_dir()
        unresolved = _control_dir(tmp_path, "later", unresolved={}, closure={})
        reasons = dict(custody.blocked_operations(_kind(tmp_path)))
        assert "confirmed it later" in reasons[unresolved]
    finally:
        process.kill()
        process.wait(timeout=10)


def test_retire_accepts_a_reboot_and_refuses_an_unrecorded_launch(tmp_path: Path) -> None:
    rebooted = _control_dir(tmp_path, "rebooted")
    (rebooted / "operation.json").write_text(json.dumps({"boot_id": "an-earlier-boot"}))
    launching = _control_dir(tmp_path, "launching")
    reports = {r.work: r for r in custody.retire_blocked(_kind(tmp_path), confirm=False)}
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
    kind = _kind(tmp_path)
    big = _control_dir(tmp_path, "big", closure={})
    (big / "stderr.log").write_bytes(b"x" * 500)
    entry = custody.quarantine(kind, big, "failure")
    assert custody.quarantine_entries(root) == [entry]  # over both bounds, newest kept


def test_restore_quarantine_removes_plaintext_and_keeps_evidence(tmp_path: Path) -> None:
    worker = custody.OperationWorker(os.getpid(), _current())
    partial = tmp_path / "restore" / ".20260926T000000Z-abcdef.partial"
    for plaintext in ("sandbox/data", "archive", "quarantine/wal", "socket"):
        (partial / plaintext).mkdir(parents=True)
    (partial / "sandbox" / "data" / "PG_VERSION").write_text("17")
    (partial / "archive" / "000000010000000000000001").write_bytes(b"WAL")
    (partial / "sandbox-postgres.log").write_text("redo done")
    owner = tmp_path / "restore-owners" / "20260926T000000Z-abcdef.owner.json"
    owner.parent.mkdir()
    owner.write_text(json.dumps({"partial": str(partial), "native": _current().value()}))
    work = tmp_path / "work"
    work.mkdir()
    restore_proof.quarantine_restore_staging(tmp_path, work, worker)
    assert not partial.exists() and not owner.exists()
    kept = work / "business" / "restore-20260926T000000Z-abcdef"
    assert sorted(path.name for path in kept.iterdir()) == ["sandbox-postgres.log"]
    assert (work / "business" / owner.name).is_file()
    restore_proof.reconcile_restore_runtime(tmp_path)  # the next proof may start


def test_base_quarantine_drops_incomplete_copies_and_keeps_resumable_captures(
    tmp_path: Path,
) -> None:
    worker = custody.OperationWorker(os.getpid(), _current())
    facts, plans = tmp_path / "base-facts", tmp_path / "base-plans"
    candidates = tmp_path / "base-candidates"
    for directory in (facts, plans, candidates):
        directory.mkdir()
    for chain, tree in (("20260926T000000Z", ".{}.partial"), ("20260927T000000Z", "{}.ready")):
        (candidates / tree.format(chain) / "base").mkdir(parents=True)
        (facts / f"{chain}.json").write_text("{}")
        (plans / f"{chain}.plan.json").write_text("{}")
        owner = {"chain_id": chain, "native": _current().value()}
        (facts / f"{chain}.owner.json").write_text(json.dumps(owner))
    work = tmp_path / "work"
    work.mkdir()
    base_candidate.quarantine_candidate_staging(tmp_path, work, worker)
    assert [path.name for path in candidates.iterdir()] == ["20260927T000000Z.ready"]
    assert sorted(path.name for path in facts.iterdir()) == ["20260927T000000Z.json"]
    assert [path.name for path in plans.iterdir()] == ["20260927T000000Z.plan.json"]
    assert len(list((work / "business").glob("*.owner.json"))) == 2
    base_candidate._recover_owned_partials(tmp_path)  # a resuming worker may start


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


def test_worker_bootstrap_refuses_code_outside_its_root(tmp_path: Path) -> None:
    """Workers run the controller's checkout, never another editable install."""
    from services.pitr import worker_process

    _BOOT = [sys.executable, "-I", "-B", "-c", worker_process._BOOTSTRAP]  # noqa: N806
    foreign = subprocess.run(  # noqa: S603
        [*_BOOT, str(tmp_path), "services.pitr.base_worker", "request", "result"],
        capture_output=True,
        text=True,
        check=False,
        cwd=tmp_path,
    )
    assert foreign.returncode != 0 and "code root mismatch" in foreign.stderr
    own = subprocess.run(  # noqa: S603
        [*_BOOT, str(worker_process._CODE_ROOT), "services.pitr.base_worker", "request", "result"],
        capture_output=True,
        text=True,
        check=False,
        cwd=tmp_path,
    )
    assert "code root mismatch" not in own.stderr and "'request'" in own.stderr


async def test_launch_failure_quarantines_and_the_next_run_proceeds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from services.pitr import worker_process
    from shared.exec_process_domain import ExecProcessDomain

    def exhausted(*_args: object, **_kwargs: object) -> None:
        raise OSError(24, "Too many open files")

    monkeypatch.setattr(ExecProcessDomain, "launch_posix", exhausted)
    for _attempt in range(2):
        with pytest.raises(OSError, match="Too many open files"):
            await worker_process.run_operation("m", {}, kind=_kind(tmp_path), env={})
    entries = custody.quarantine_entries(tmp_path / "quarantine")
    assert len(entries) == 2 and not list((tmp_path / "controls").glob(".operation-*"))
    assert json.loads((entries[0] / "closure.json").read_text())["proven_by"] == "no-process"


async def test_worker_secrets_and_progress_never_touch_retained_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The live URL arrives on stdin; stderr progress reaches the operator live."""
    from services.pitr import worker_process
    from tests.services.test_pitr_operation_owner import _worker

    _worker(
        tmp_path,
        monkeypatch,
        "import hashlib\nfrom services.pitr.worker_process import worker_secrets\n"
        "seen=hashlib.sha256(worker_secrets()['live_db_url'].encode()).hexdigest()\n"
        "sys.stderr.write('downloading base\\nbase extracted\\n');sys.stderr.flush()\n"
        "Path(sys.argv[2]).write_text(json.dumps({'seen':seen}))\n",
    )
    lines: list[str] = []
    secret = "postgresql://ava:s3cret@127.0.0.1:5433/ava"  # noqa: S105 -- fixture
    completed = await worker_process.run_operation(
        "m",
        {"chain": "c"},
        kind=_kind(tmp_path),
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
    from services.pitr import worker_process
    from shared.exec_process_domain import ExecProcessDomain
    from tests.services.test_pitr_operation_owner import _worker

    _worker(tmp_path, monkeypatch, "Path(sys.argv[2]).write_text('{}')\n")
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
        completed = await worker_process.run_operation("m", {}, kind=_kind(tmp_path), env={})
    finally:
        ticker.cancel()
    await completed.commit(lambda: None)
    assert during and during[0] >= 5


async def test_activation_cancel_names_an_unresolved_closure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Lease loss never hides blocked restore controls behind "cancelled"."""
    import threading

    from services.pitr import activation_runtime, base_operation_runtime
    from tests.services.test_pitr_base_scheduler import _candidate

    async def unresolved(_candidate: object) -> dict[str, str]:
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError as cancelled:
            raise cancelled from custody.OperationCustodyError(Path("/controls/.operation-x"))
        return {}

    monkeypatch.setattr(base_operation_runtime, "run_restore", unresolved)
    stop = threading.Event()
    stop.set()
    with pytest.raises(RuntimeError, match="closure is unresolved") as caught:
        await activation_runtime._restore_activation_candidate(_candidate("c"), stop)
    assert isinstance(caught.value.__cause__, custody.OperationCustodyError)


async def test_cancelled_drill_gets_time_to_write_its_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stopped drill stops its sandbox (up to 20 s) before writing evidence;
    its grace covers that instead of killing it after three seconds."""
    from services.pitr import base_operation_runtime
    from tests.services.test_pitr_operation_owner import _restore_inputs, _worker

    started, scratch = tmp_path / "started", tmp_path / "scratch"
    _worker(
        tmp_path,
        monkeypatch,
        "from services.pitr.worker_process import worker_request\n"
        "request, output = worker_request(sys.argv)\n"
        "scratch=Path(request['drill']['scratch']);scratch.mkdir()\n"
        "try:\n"
        f" Path({str(started)!r}).write_text('1');time.sleep(60)\n"
        "finally:\n"
        " time.sleep(4);(scratch/'drill-evidence.json').write_text('{\"outcome\":\"fail\"}')\n",
    )
    task = asyncio.create_task(
        base_operation_runtime.run_drill_input(
            _restore_inputs(tmp_path),
            scratch=scratch,
            target_lsn="0/180",
            target_wall="2026-09-26T00:00:00+00:00",
            timeout_seconds=60,
        )
    )
    deadline = time.monotonic() + 10
    while not started.exists() and time.monotonic() < deadline:
        await asyncio.sleep(0.02)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 30)
    assert json.loads((scratch / "drill-evidence.json").read_text()) == {"outcome": "fail"}
    assert len(custody.quarantine_entries(tmp_path / "quarantine")) == 1
