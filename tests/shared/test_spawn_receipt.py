"""Tests for shared.spawn_receipt — the exact-spawn adjudicator.

These exercise the real filesystem + flock semantics (and, where marked, real
Linux /proc identities); the process-level gated-spawn tests spawn REAL
detached children through the native supervisor, so gate inheritance and the
child-written birth receipt are observed end to end rather than mocked.
"""

from __future__ import annotations

import json
import os
import subprocess
import threading
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

import psutil
import pytest

from shared import spawn_receipt
from shared.platform import IS_LINUX, IS_WINDOWS
from shared.proc_tree import stable_create_time
from shared.session_backend import PosixProcSessionBackend, get_backend
from shared.session_record import SessionRecord, pid_starttime_ticks
from shared.spawn_receipt import (
    SpawnEvidenceInvalidError,
    SpawnExitedError,
    SpawnExpectation,
    SpawnReceipt,
    SpawnRefusedError,
)
from tests.shared.poll_until import poll_until

pytestmark = pytest.mark.skipif(IS_WINDOWS, reason="spawn gate/receipt primitives are POSIX")

linux_only = pytest.mark.skipif(not IS_LINUX, reason="Linux /proc process identity")

_GENERATION = "gen-20260920"
_SESSION = "ava-spawn-test"
_SLEEP = "exec /bin/sleep 300"


def _expectation(
    nonce: uuid.UUID,
    *,
    home: Path,
    session: str = _SESSION,
    command: str = _SLEEP,
    cwd: str | None = None,
) -> SpawnExpectation:
    import hashlib

    return SpawnExpectation(
        nonce=nonce,
        session=session,
        home=str(home),
        machine="test-machine",
        cmd_digest=hashlib.sha256(command.encode()).hexdigest(),
        cwd=cwd if cwd is not None else str(home),
    )


def _receipt_file(home: Path, nonce: uuid.UUID, session: str = _SESSION) -> Path:
    return spawn_receipt.receipt_path(home, _GENERATION, session, nonce)


def _gate(home: Path, session: str = _SESSION) -> Path:
    return spawn_receipt.session_lock_path(home, _GENERATION, session)


def _birth_receipt(
    expectation: SpawnExpectation,
    *,
    pid: int,
    create_time: float | None = None,
    starttime: int | None = None,
) -> SpawnReceipt:
    if create_time is None or starttime is None:
        create_time = stable_create_time(psutil.Process(pid))
        starttime = pid_starttime_ticks(pid)
    return SpawnReceipt(
        kind="birth",
        **expectation.model_dump(),
        pid=pid,
        create_time=create_time,
        starttime=starttime,
        captured_at=datetime.now(UTC),
    )


def _await(
    home: Path,
    expectation: SpawnExpectation,
    *,
    budget: float = 5.0,
) -> spawn_receipt.SpawnOutcome:
    return spawn_receipt.await_birth(
        _receipt_file(home, expectation.nonce, expectation.session),
        expectation,
        _gate(home, expectation.session),
        deadline=time.monotonic() + budget,
        poll_s=0.01,
    )


def _publish(receipt_file: Path, receipt: SpawnReceipt) -> None:
    """Publish a receipt the way both real writers do: atomically."""
    spawn_receipt._write_atomic_text(receipt_file, receipt.model_dump_json())


# ── receipt model + paths ──────────────────────────────────────────────────


def test_intent_and_birth_receipts_are_kind_coherent(tmp_path: Path) -> None:
    expectation = _expectation(uuid.uuid4(), home=tmp_path)
    with pytest.raises(ValueError):
        SpawnReceipt(kind="birth", **expectation.model_dump())
    with pytest.raises(ValueError):
        SpawnReceipt(kind="intent", **expectation.model_dump(), pid=1, create_time=1.0, starttime=1)
    with pytest.raises(ValueError):
        SpawnReceipt(
            kind="birth",
            **expectation.model_dump(),
            pid=1,
            create_time=1.0,
            starttime=None,
            captured_at=datetime.now(UTC),
        )


def test_receipt_rejects_unknown_fields_and_binds_by_facts(tmp_path: Path) -> None:
    expectation = _expectation(uuid.uuid4(), home=tmp_path)
    body = json.loads(SpawnReceipt(kind="intent", **expectation.model_dump()).model_dump_json())
    body["extra"] = "not allowed"
    with pytest.raises(ValueError):
        SpawnReceipt.model_validate_json(json.dumps(body))
    intent = SpawnReceipt(kind="intent", **expectation.model_dump())
    assert intent.matches(expectation)
    assert not intent.matches(_expectation(uuid.uuid4(), home=tmp_path))


def test_attempt_paths_are_fixed_shape_and_checked(tmp_path: Path) -> None:
    nonce = uuid.uuid4()
    root = tmp_path / "run" / "updater-spawn" / _GENERATION
    assert _receipt_file(tmp_path, nonce) == root / f"{_SESSION}.{nonce}.receipt.json"
    assert _gate(tmp_path) == root / f"{_SESSION}.gate"
    with pytest.raises(ValueError):
        spawn_receipt.receipt_path(tmp_path, _GENERATION, "../escape", nonce)
    with pytest.raises(ValueError):
        spawn_receipt.session_lock_path(tmp_path, "gen/../escape", _SESSION)


def test_write_intent_is_a_bound_durable_intent(tmp_path: Path) -> None:
    nonce = uuid.uuid4()
    receipt_file = _receipt_file(tmp_path, nonce)
    expectation = _expectation(nonce, home=tmp_path)
    spawn_receipt.write_intent(receipt_file, expectation)
    receipt = spawn_receipt._read_receipt_file(receipt_file, limit=64 * 1024)
    assert receipt is not None and receipt.kind == "intent"
    assert receipt.matches(expectation)
    assert receipt.pid is None and receipt.captured_at is None
    assert (receipt_file.stat().st_mode & 0o777) == 0o600


# ── gate semantics (D1/D2: why release is close-only) ──────────────────────


def test_lock_un_releases_every_shared_description(tmp_path: Path) -> None:
    """D1/INJ-17: LOCK_UN on one descriptor releases an inherited copy too.

    The dup'd descriptor shares the open file description exactly the way a
    fork-inherited one does — which is why release is os.close-only.
    """
    import fcntl

    gate = _gate(tmp_path)
    fd = spawn_receipt.take_session_lock(gate)
    inherited = os.dup(fd)
    try:
        assert spawn_receipt.probe_session_lock_free(gate) is False
        fcntl.flock(fd, fcntl.LOCK_UN)
        assert spawn_receipt.probe_session_lock_free(gate) is True
    finally:
        os.close(fd)
        os.close(inherited)


def test_close_only_release_keeps_the_shared_description(tmp_path: Path) -> None:
    """D2: closing one descriptor leaves the shared description held."""
    gate = _gate(tmp_path)
    fd = spawn_receipt.take_session_lock(gate)
    inherited = os.dup(fd)
    os.close(fd)
    try:
        assert spawn_receipt.probe_session_lock_free(gate) is False
    finally:
        os.close(inherited)
    assert spawn_receipt.probe_session_lock_free(gate) is True


def test_take_session_lock_refuses_while_held(tmp_path: Path) -> None:
    gate = _gate(tmp_path)
    fd = spawn_receipt.take_session_lock(gate)
    try:
        with pytest.raises(SpawnRefusedError):
            spawn_receipt.take_session_lock(gate)
    finally:
        os.close(fd)
    os.close(spawn_receipt.take_session_lock(gate))


# ── adjudication decision table ────────────────────────────────────────────


def test_absent_receipt_with_free_gate_is_not_spawned(unit_home: Path) -> None:
    expectation = _expectation(uuid.uuid4(), home=unit_home)
    outcome = _await(unit_home, expectation)
    assert outcome.verdict == "not_spawned"
    assert outcome.receipt is None


def test_intent_with_free_gate_is_not_spawned(unit_home: Path) -> None:
    """Crash after the intent write, before any lineage process: retryable."""
    nonce = uuid.uuid4()
    expectation = _expectation(nonce, home=unit_home)
    spawn_receipt.write_intent(_receipt_file(unit_home, nonce), expectation)
    outcome = _await(unit_home, expectation)
    assert outcome.verdict == "not_spawned"
    assert outcome.receipt is not None and outcome.receipt.kind == "intent"


def test_held_gate_without_receipt_times_out_ambiguous(unit_home: Path) -> None:
    """N3 shape: an externally held gate is conservative — never not_spawned."""
    nonce = uuid.uuid4()
    expectation = _expectation(nonce, home=unit_home)
    fd = spawn_receipt.take_session_lock(_gate(unit_home))
    try:
        outcome = _await(unit_home, expectation, budget=0.2)
        assert outcome.verdict == "ambiguous"
        assert outcome.receipt is None
    finally:
        os.close(fd)


def test_gate_release_during_wait_is_not_spawned(unit_home: Path) -> None:
    """The lineage dies pre-birth: the wait observes the gate freeing."""
    nonce = uuid.uuid4()
    expectation = _expectation(nonce, home=unit_home)
    holder = {"fd": spawn_receipt.take_session_lock(_gate(unit_home))}

    def release() -> None:
        time.sleep(0.15)
        os.close(holder.pop("fd"))

    thread = threading.Thread(target=release)
    thread.start()
    try:
        outcome = _await(unit_home, expectation)
        assert outcome.verdict == "not_spawned"
    finally:
        thread.join()
    assert not holder


def test_corrupt_receipt_is_ambiguous_not_absent(unit_home: Path) -> None:
    """N4 shape: damaged is never folded to absent."""
    nonce = uuid.uuid4()
    expectation = _expectation(nonce, home=unit_home)
    receipt_file = _receipt_file(unit_home, nonce)
    receipt_file.parent.mkdir(parents=True, exist_ok=True)
    receipt_file.write_text("{not-json")
    outcome = _await(unit_home, expectation)
    assert outcome.verdict == "ambiguous"
    assert outcome.receipt is None


def test_oversize_receipt_is_refused(tmp_path: Path) -> None:
    receipt_file = _receipt_file(tmp_path, uuid.uuid4())
    receipt_file.parent.mkdir(parents=True, exist_ok=True)
    receipt_file.write_bytes(b"x" * 8)
    with pytest.raises(SpawnEvidenceInvalidError):
        spawn_receipt._read_receipt_file(receipt_file, limit=4)


def test_receipt_absent_is_not_an_error(tmp_path: Path) -> None:
    assert spawn_receipt._read_receipt_file(_receipt_file(tmp_path, uuid.uuid4()), limit=64) is None


def test_unbound_receipt_is_ambiguous(unit_home: Path) -> None:
    """N2 shape: a receipt whose facts differ from the attempt is refused."""
    nonce = uuid.uuid4()
    expectation = _expectation(nonce, home=unit_home)
    other = _expectation(nonce, home=unit_home, command="exec /bin/other")
    spawn_receipt.write_intent(_receipt_file(unit_home, nonce), other)
    outcome = _await(unit_home, expectation)
    assert outcome.verdict == "ambiguous"
    assert outcome.receipt is None


@linux_only
def test_birth_receipt_with_live_child_is_spawned_alive(unit_home: Path) -> None:
    process = subprocess.Popen(["/bin/sleep", "300"])
    try:
        nonce = uuid.uuid4()
        expectation = _expectation(nonce, home=unit_home)
        _publish(_receipt_file(unit_home, nonce), _birth_receipt(expectation, pid=process.pid))
        outcome = _await(unit_home, expectation)
        assert outcome.verdict == "spawned_alive"
        assert outcome.receipt is not None and outcome.receipt.pid == process.pid
    finally:
        process.kill()
        process.wait()


@linux_only
def test_birth_receipt_for_exited_child_is_record_adjudicated_dead(unit_home: Path) -> None:
    process = subprocess.Popen(["/bin/sleep", "300"])
    try:
        nonce = uuid.uuid4()
        expectation = _expectation(nonce, home=unit_home)
        receipt = _birth_receipt(expectation, pid=process.pid)
        process.kill()
        process.wait()
        _publish(_receipt_file(unit_home, nonce), receipt)
        outcome = _await(unit_home, expectation)
        assert outcome.verdict == "spawned_dead"
        assert outcome.receipt is not None
        assert "exited" in outcome.reason
    finally:
        process.kill()
        process.wait()


@linux_only
def test_birth_receipt_with_reused_pid_is_record_adjudicated_dead(unit_home: Path) -> None:
    """The pid's next occupant is a different birth: the original child is dead."""
    process = subprocess.Popen(["/bin/sleep", "300"])
    try:
        nonce = uuid.uuid4()
        expectation = _expectation(nonce, home=unit_home)
        receipt = _birth_receipt(
            expectation,
            pid=process.pid,
            create_time=stable_create_time(psutil.Process(process.pid)),
            starttime=(pid_starttime_ticks(process.pid) or 1) + 1,
        )
        _publish(_receipt_file(unit_home, nonce), receipt)
        outcome = _await(unit_home, expectation)
        assert outcome.verdict == "spawned_dead"
        assert "different birth" in outcome.reason
    finally:
        process.kill()
        process.wait()


@linux_only
def test_birth_appearing_during_wait_is_spawned_alive(unit_home: Path) -> None:
    """The held gate stays held while the pre-birth child publishes its birth."""
    nonce = uuid.uuid4()
    expectation = _expectation(nonce, home=unit_home)
    receipt_file = _receipt_file(unit_home, nonce)
    fd = spawn_receipt.take_session_lock(_gate(unit_home))
    process = subprocess.Popen(["/bin/sleep", "300"])
    receipt = _birth_receipt(expectation, pid=process.pid)

    def publish() -> None:
        time.sleep(0.15)
        _publish(receipt_file, receipt)

    thread = threading.Thread(target=publish)
    thread.start()
    try:
        outcome = _await(unit_home, expectation)
        assert outcome.verdict == "spawned_alive"
    finally:
        thread.join()
        os.close(fd)
        process.kill()
        process.wait()


# ── record cross-check and the one privileged repair ──────────────────────


def test_read_session_record_distinguishes_missing_from_damaged(unit_home: Path) -> None:
    assert spawn_receipt.read_session_record(unit_home, "ava-absent") is None
    path = unit_home / "run" / "sessions" / "ava-damaged.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{broken")
    with pytest.raises(SpawnEvidenceInvalidError):
        spawn_receipt.read_session_record(unit_home, "ava-damaged")


def test_record_matches_receipt_exact_starttime_then_tolerance(tmp_path: Path) -> None:
    expectation = _expectation(uuid.uuid4(), home=tmp_path)
    receipt = SpawnReceipt(
        kind="birth",
        **expectation.model_dump(),
        pid=10,
        create_time=100.0,
        starttime=500,
        captured_at=datetime.now(UTC),
    )
    exact = SessionRecord(
        pid=10, create_time=100.5, cmd=_SLEEP, cwd=str(tmp_path), started_at=1.0, starttime=500
    )
    assert spawn_receipt.record_matches_receipt(exact, receipt)
    wrong_birth = SessionRecord(
        pid=10, create_time=100.5, cmd=_SLEEP, cwd=str(tmp_path), started_at=1.0, starttime=501
    )
    assert not spawn_receipt.record_matches_receipt(wrong_birth, receipt)
    legacy = SessionRecord(pid=10, create_time=101.5, cmd=_SLEEP, cwd=str(tmp_path), started_at=1.0)
    assert spawn_receipt.record_matches_receipt(legacy, receipt)
    legacy_far = SessionRecord(
        pid=10, create_time=95.0, cmd=_SLEEP, cwd=str(tmp_path), started_at=1.0
    )
    assert not spawn_receipt.record_matches_receipt(legacy_far, receipt)
    other_pid = SessionRecord(
        pid=11, create_time=100.5, cmd=_SLEEP, cwd=str(tmp_path), started_at=1.0, starttime=500
    )
    assert not spawn_receipt.record_matches_receipt(other_pid, receipt)


def test_write_recovered_record_refuses_a_damaged_record(tmp_path: Path) -> None:
    expectation = _expectation(uuid.uuid4(), home=tmp_path)
    receipt = SpawnReceipt(
        kind="birth",
        **expectation.model_dump(),
        pid=11,
        create_time=100.0,
        starttime=500,
        captured_at=datetime.now(UTC),
    )
    path = tmp_path / "run" / "sessions" / f"{_SESSION}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{broken")
    with pytest.raises(SpawnEvidenceInvalidError):
        spawn_receipt.write_recovered_record(
            tmp_path,
            _SESSION,
            receipt,
            command=_SLEEP,
            cwd=tmp_path,
            generation=_GENERATION,
        )


@linux_only
def test_write_recovered_record_repairs_once_and_verifies(unit_home: Path) -> None:
    process = subprocess.Popen(["/bin/sleep", "300"])
    try:
        expectation = _expectation(uuid.uuid4(), home=unit_home)
        receipt = _birth_receipt(expectation, pid=process.pid)
        record = spawn_receipt.write_recovered_record(
            unit_home,
            _SESSION,
            receipt,
            command=_SLEEP,
            cwd=unit_home,
            generation=_GENERATION,
        )
        assert record.pid == process.pid
        assert receipt.captured_at is not None
        assert record.started_at == receipt.captured_at.timestamp()
        assert spawn_receipt.read_session_record(unit_home, _SESSION) == record
        assert spawn_receipt.record_matches_receipt(record, receipt)
        with pytest.raises(SpawnEvidenceInvalidError):
            spawn_receipt.write_recovered_record(
                unit_home,
                _SESSION,
                receipt,
                command=_SLEEP,
                cwd=unit_home,
                generation=_GENERATION,
            )
    finally:
        process.kill()
        process.wait()


@linux_only
def test_write_recovered_record_reports_a_dead_child(unit_home: Path) -> None:
    process = subprocess.Popen(["/bin/sleep", "300"])
    try:
        receipt = _birth_receipt(_expectation(uuid.uuid4(), home=unit_home), pid=process.pid)
        process.kill()
        process.wait()
        with pytest.raises(SpawnExitedError):
            spawn_receipt.write_recovered_record(
                unit_home,
                _SESSION,
                receipt,
                command=_SLEEP,
                cwd=unit_home,
                generation=_GENERATION,
            )
    finally:
        process.kill()
        process.wait()


# ── the gated spawn itself ─────────────────────────────────────────────────


class _MustNotRunBackend(PosixProcSessionBackend):
    def new_session(
        self,
        name: str,
        cmd: str,
        cwd: Path,
        *,
        env: dict[str, str],
        login_shell: bool = True,
        exec_cmd: bool = True,
        gate_fd: int | None = None,
        receipt: tuple[Path, str] | None = None,
    ) -> bool:
        raise AssertionError("the platform gate must refuse before any backend call")


@pytest.mark.skipif(IS_LINUX, reason="the Linux gate is the tested path")
def test_execute_gated_spawn_refuses_off_linux(unit_home: Path) -> None:
    nonce = uuid.uuid4()
    with pytest.raises(SpawnRefusedError):
        spawn_receipt.execute_gated_spawn(
            _MustNotRunBackend(),
            name=_SESSION,
            command=_SLEEP,
            workdir=unit_home,
            env=dict(os.environ),
            home=unit_home,
            generation=_GENERATION,
            machine="test-machine",
            nonce=nonce,
            wait_budget=1.0,
        )
    assert not _receipt_file(unit_home, nonce).exists()
    assert not _gate(unit_home).exists()


@pytest.mark.skipif(IS_LINUX, reason="the fail-closed child path is the non-Linux path")
def test_gated_child_without_proc_identity_never_execs(unit_home: Path) -> None:
    """B-2 fail-closed: no /proc identity -> the child exits without exec and
    the receipt stays an intent (the attempt can then never be "born")."""
    nonce = uuid.uuid4()
    expectation = _expectation(nonce, home=unit_home)
    receipt_file = _receipt_file(unit_home, nonce)
    spawn_receipt.write_intent(receipt_file, expectation)
    fd = spawn_receipt.take_session_lock(_gate(unit_home))
    try:
        from shared import posixproc

        assert posixproc.new_session(
            _SESSION,
            ["/bin/sleep", "300"],
            unit_home,
            env=dict(os.environ),
            gate_fd=fd,
            receipt=(receipt_file, str(nonce)),
        )
    finally:
        os.close(fd)
    record = spawn_receipt.read_session_record(unit_home, _SESSION)
    assert record is not None
    # The child exits in the receipt step: were the exec reached, /bin/sleep
    # 300 would still hold this pid and the wait would time out.
    poll_until(
        lambda: not psutil.pid_exists(record.pid),
        what="the receipt-less child exits without exec",
    )
    receipt = spawn_receipt._read_receipt_file(receipt_file, limit=64 * 1024)
    assert receipt is not None and receipt.kind == "intent"
    assert spawn_receipt.probe_session_lock_free(_gate(unit_home)) is True


def test_posixproc_refuses_a_half_gated_launch(unit_home: Path) -> None:
    """A gate without a receipt (or vice versa) is refused before any effect."""
    from shared import posixproc

    receipt_file = _receipt_file(unit_home, uuid.uuid4())
    with pytest.raises(ValueError):
        posixproc.new_session(
            _SESSION,
            ["/bin/sleep", "300"],
            unit_home,
            env={},
            gate_fd=os.open(os.devnull, os.O_RDONLY),
        )
    with pytest.raises(ValueError):
        posixproc.new_session(
            _SESSION,
            ["/bin/sleep", "300"],
            unit_home,
            env={},
            receipt=(receipt_file, "nonce"),
        )
    assert not receipt_file.exists()


@linux_only
def test_gated_spawn_birth_gate_and_record(unit_home: Path) -> None:
    """A real detached child: birth receipt written by the child, gate held
    until death (INJ-16's positive form), record cross-checked."""
    nonce = uuid.uuid4()
    expectation = _expectation(nonce, home=unit_home)
    receipt = spawn_receipt.execute_gated_spawn(
        PosixProcSessionBackend(),
        name=_SESSION,
        command=_SLEEP,
        workdir=unit_home,
        env=dict(os.environ),
        home=unit_home,
        generation=_GENERATION,
        machine="test-machine",
        nonce=nonce,
        wait_budget=10.0,
    )
    assert receipt.kind == "birth"
    assert receipt.matches(expectation)
    assert receipt.pid is not None and receipt.starttime is not None
    gate = _gate(unit_home)
    try:
        assert spawn_receipt.probe_session_lock_free(gate) is False
        record = spawn_receipt.read_session_record(unit_home, _SESSION)
        assert record is not None
        assert spawn_receipt.record_matches_receipt(record, receipt)
        assert record.cmd == _SLEEP
        assert '"kind":"birth"' in _receipt_file(unit_home, nonce).read_text()
    finally:
        get_backend().kill_session(_SESSION, graceful=False)
    poll_until(
        lambda: spawn_receipt.probe_session_lock_free(gate),
        what="the session gate frees once the child is dead",
    )


@linux_only
def test_gated_spawn_adopts_when_the_helper_report_is_lost(unit_home: Path) -> None:
    """The receipt is the verdict: a lost helper report after the fork must
    not become a lost child."""

    class _LostReportBackend(PosixProcSessionBackend):
        def new_session(
            self,
            name: str,
            cmd: str,
            cwd: Path,
            *,
            env: dict[str, str],
            login_shell: bool = True,
            exec_cmd: bool = True,
            gate_fd: int | None = None,
            receipt: tuple[Path, str] | None = None,
        ) -> bool:
            super().new_session(
                name,
                cmd,
                cwd,
                env=env,
                login_shell=login_shell,
                exec_cmd=exec_cmd,
                gate_fd=gate_fd,
                receipt=receipt,
            )
            raise RuntimeError("helper report lost after the fork")

    nonce = uuid.uuid4()
    session = "ava-spawn-lost"
    try:
        receipt = spawn_receipt.execute_gated_spawn(
            _LostReportBackend(),
            name=session,
            command=_SLEEP,
            workdir=unit_home,
            env=dict(os.environ),
            home=unit_home,
            generation=_GENERATION,
            machine="test-machine",
            nonce=nonce,
            wait_budget=10.0,
        )
        assert receipt.kind == "birth"
        assert spawn_receipt.probe_session_lock_free(_gate(unit_home, session)) is False
    finally:
        get_backend().kill_session(session, graceful=False)


@linux_only
def test_gated_spawn_child_that_cannot_exec_still_mints_its_birth(unit_home: Path) -> None:
    """The child writes its birth before exec; an exec failure afterwards is
    adjudicated from that receipt, never as "no lineage at all"."""
    nonce = uuid.uuid4()
    session = "ava-spawn-missing"
    try:
        returned = spawn_receipt.execute_gated_spawn(
            PosixProcSessionBackend(),
            name=session,
            command="exec /nonexistent/ava-test-binary",
            workdir=unit_home,
            env=dict(os.environ),
            home=unit_home,
            generation=_GENERATION,
            machine="test-machine",
            nonce=nonce,
            wait_budget=10.0,
        )
        assert returned.kind == "birth"
    except SpawnExitedError:
        pass  # the child may already be dead when adjudication runs; both verdicts are honest
    receipt_file = spawn_receipt.receipt_path(unit_home, _GENERATION, session, nonce)
    receipt = spawn_receipt._read_receipt_file(receipt_file, limit=64 * 1024)
    assert receipt is not None and receipt.kind == "birth"
    poll_until(
        lambda: spawn_receipt.probe_session_lock_free(_gate(unit_home, session)),
        what="the session gate frees after the exec failure",
    )
