"""PITR native custody survives clock correction and refuses unknown owners."""

from __future__ import annotations

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
from services.pitr.restore_postgres import SandboxPostgresIdentity
from services.pitr.restore_proof import LivePostgresIdentity, RestoreProofError
from services.pitr.worker_process import NativeProcess
from shared.native_process import ownership
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
