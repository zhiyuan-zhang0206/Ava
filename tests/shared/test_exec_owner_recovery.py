"""Durable resource recovery requires boot-scoped native identity, never wall time."""

from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import psutil
import pytest
from pydantic import ValidationError

from shared import exec_owner_recovery, native_process
from shared.exec_owner_recovery import process_ended
from shared.incarnation_resources import (
    IncarnationResources,
    ResourceEvidenceError,
    ResourceProcess,
)
from shared.native_process import ownership as proc_tree

_BOOT = "11111111-1111-4111-8111-111111111111"
_OTHER_BOOT = "22222222-2222-4222-8222-222222222222"


@pytest.fixture
def linux_birth(monkeypatch: pytest.MonkeyPatch) -> dict[str, float | int]:
    observed: dict[str, float | int] = {"birth": 100.0, "tick": 70}

    def birth(_process: psutil.Process) -> float:
        return float(observed["birth"])

    def ticks(_pid: int) -> int:
        return int(observed["tick"])

    monkeypatch.setattr(proc_tree.sys, "platform", "linux")
    monkeypatch.setattr(native_process, "native_boot_id", lambda: _BOOT)
    monkeypatch.setattr(proc_tree, "stable_create_time", birth)
    monkeypatch.setattr(proc_tree, "pid_starttime_ticks", ticks)
    return observed


def test_exact_native_process_has_not_ended() -> None:
    assert not process_ended(ResourceProcess.capture(psutil.Process()))


def test_linux_clock_movement_cannot_end_or_replace_a_live_host(
    linux_birth: dict[str, float | int],
) -> None:
    original = ResourceProcess.capture(psutil.Process())
    linux_birth["birth"] += 600.0
    fresh = ResourceProcess.capture(psutil.Process())
    assert original != fresh, "durable receipt equality must remain exact"
    assert original.same_birth(fresh)
    assert not process_ended(original)


def test_linux_exact_tick_reuse_proves_the_old_process_ended(
    linux_birth: dict[str, float | int],
) -> None:
    original = ResourceProcess.capture(psutil.Process())
    linux_birth["tick"] += 1
    assert process_ended(original)


@pytest.mark.parametrize("missing", ["starttime", "boot_id"])
def test_incomplete_linux_receipt_never_proves_absence(
    linux_birth: dict[str, float | int], monkeypatch: pytest.MonkeyPatch, missing: str
) -> None:
    original = ResourceProcess.capture(psutil.Process()).model_copy(update={missing: None})

    def no_observation(_pid: int) -> psutil.Process:
        raise AssertionError("incomplete evidence reached a PID observation")

    monkeypatch.setattr(exec_owner_recovery.psutil, "Process", no_observation)
    assert not process_ended(original)
    with pytest.raises(ResourceEvidenceError, match="native evidence"):
        original.as_identity()


def test_unavailable_live_tick_is_unknown_not_ended(
    linux_birth: dict[str, float | int], monkeypatch: pytest.MonkeyPatch
) -> None:
    original = ResourceProcess.capture(psutil.Process())

    def missing_ticks(_pid: int) -> None:
        return None

    monkeypatch.setattr(proc_tree, "pid_starttime_ticks", missing_ticks)
    assert not process_ended(original)


def test_another_boot_proves_prior_process_ended_without_adopting_pid(
    linux_birth: dict[str, float | int], monkeypatch: pytest.MonkeyPatch
) -> None:
    original = ResourceProcess.capture(psutil.Process())
    monkeypatch.setattr(native_process, "native_boot_id", lambda: _OTHER_BOOT)

    def no_observation(_pid: int) -> psutil.Process:
        raise AssertionError("prior boot must not inspect the replacement PID")

    monkeypatch.setattr(exec_owner_recovery.psutil, "Process", no_observation)
    assert process_ended(original)
    with pytest.raises(ResourceEvidenceError, match="another boot"):
        original.as_identity()


def test_boot_observation_failure_cannot_prove_exit(
    linux_birth: dict[str, float | int], monkeypatch: pytest.MonkeyPatch
) -> None:
    original = ResourceProcess.capture(psutil.Process())

    def unavailable() -> str:
        raise OSError("boot scope unavailable")

    monkeypatch.setattr(native_process, "native_boot_id", unavailable)
    assert not process_ended(original)


@pytest.mark.parametrize("platform", ["darwin", "win32"])
def test_nonlinux_native_timestamp_is_exact(monkeypatch: pytest.MonkeyPatch, platform: str) -> None:
    monkeypatch.setattr(proc_tree.sys, "platform", platform)
    boot = None if platform == "win32" else _BOOT
    monkeypatch.setattr(native_process, "native_boot_id", lambda: boot)
    original = ResourceProcess(pid=psutil.Process().pid, birth=100.0, starttime=None, boot_id=boot)
    assert original.same_birth(original.model_copy())
    assert not original.same_birth(original.model_copy(update={"birth": 100.001}))


def test_wire_receipt_requires_explicit_native_fields() -> None:
    with pytest.raises(ValidationError):
        ResourceProcess.model_validate_json('{"pid":41,"birth":100.0}')
    with pytest.raises(ValidationError, match="canonical"):
        ResourceProcess(
            pid=41, birth=100.0, starttime=70, boot_id=_BOOT.upper().replace("1111", "AAAA", 1)
        )


def test_same_host_admission_keeps_original_receipt_after_clock_movement(
    linux_birth: dict[str, float | int],
) -> None:
    from shared.resource_admission import _next
    from shared.runtime_incarnation import RuntimeIncarnation

    original = ResourceProcess.capture(psutil.Process())
    target = RuntimeIncarnation(1, uuid4(), uuid4())
    state = IncarnationResources(
        generation=target.generation, owner=target.owner, host_process=original, requests={}
    )
    row = (
        state.model_dump(mode="json"),
        target.generation,
        target.owner,
        "hosted",
        original.pid,
        None,
        1,
    )
    linux_birth["birth"] += 60
    admitted = _next(row, target, ResourceProcess.capture(psutil.Process()), predecessor=False)
    assert admitted == state


@pytest.mark.parametrize("changed", [None, "launcher_tick", "owner_tick", "root_tick", "ancestry"])
def test_ready_handshake_uses_retained_native_launcher_and_child(
    linux_birth: dict[str, float | int], monkeypatch: pytest.MonkeyPatch, changed: str | None
) -> None:
    from datetime import UTC, datetime, timedelta

    from shared import exec_owner_protocol
    from shared.exec_owner_protocol import OwnerReady, validate_native_ready
    from shared.incarnation_resources import ExecAllocation

    owner = ResourceProcess(pid=41, birth=100.0, starttime=70, boot_id=_BOOT)
    child = ResourceProcess(pid=42, birth=100.0, starttime=71, boot_id=_BOOT)
    launcher = owner
    if changed == "launcher_tick":
        launcher = launcher.model_copy(update={"starttime": 72})
    if changed == "owner_tick":
        owner = owner.model_copy(update={"starttime": 72})
    if changed == "root_tick":
        child = child.model_copy(update={"starttime": 72})
    ready = OwnerReady(
        allocation=ExecAllocation(
            request=uuid4(),
            domain=uuid4(),
            request_digest="a" * 64,
            deadline=datetime.now(UTC) + timedelta(seconds=30),
            owner_process=owner,
            root_process=child,
        )
    )
    observed = {
        41: ResourceProcess(pid=41, birth=500.0, starttime=70, boot_id=_BOOT),
        42: ResourceProcess(pid=42, birth=500.0, starttime=71, boot_id=_BOOT),
    }

    def capture(process: psutil.Process) -> ResourceProcess:
        return observed[process.pid]

    def process(pid: int) -> SimpleNamespace:
        return SimpleNamespace(pid=pid, ppid=lambda: 99 if changed == "ancestry" else 41)

    monkeypatch.setattr(ResourceProcess, "capture", capture)
    monkeypatch.setattr(exec_owner_protocol.psutil, "Process", process)
    from pathlib import Path

    if changed is None:
        validate_native_ready(ready, launcher, Path("/private/owner.json"))
    else:
        with pytest.raises(ValueError, match=r"birth changed|direct child"):
            validate_native_ready(ready, launcher, Path("/private/owner.json"))


@pytest.mark.parametrize("changed", [None, "parent", "argv"])
def test_windows_redirector_still_requires_exact_owner_ancestry(
    monkeypatch: pytest.MonkeyPatch, changed: str | None
) -> None:
    from datetime import UTC, datetime, timedelta
    from pathlib import Path

    from shared import exec_owner_protocol
    from shared.exec_owner_protocol import OwnerReady, validate_native_ready
    from shared.incarnation_resources import ExecAllocation

    monkeypatch.setattr(proc_tree.sys, "platform", "win32")
    identities = {
        pid: ResourceProcess(pid=pid, birth=float(pid), starttime=None, boot_id=None)
        for pid in (40, 41, 42)
    }
    ready = OwnerReady(
        allocation=ExecAllocation(
            request=uuid4(),
            domain=uuid4(),
            request_digest="a" * 64,
            deadline=datetime.now(UTC) + timedelta(seconds=30),
            owner_process=identities[41],
            root_process=identities[42],
        )
    )
    context = Path("/private/owner.json")
    argv = [
        "python",
        "-I",
        "-B",
        "-X",
        "utf8",
        "-m",
        "agent.exec_domain_owner",
        "--context",
        str(context),
    ]

    def capture(process: psutil.Process) -> ResourceProcess:
        return identities[process.pid]

    def process(pid: int) -> SimpleNamespace:
        parent = 41 if pid == 42 else 99 if changed == "parent" else 40
        arguments = ["wrong"] if changed == "argv" and pid == 41 else argv
        return SimpleNamespace(pid=pid, ppid=lambda: parent, cmdline=lambda: arguments)

    monkeypatch.setattr(ResourceProcess, "capture", capture)
    monkeypatch.setattr(exec_owner_protocol.psutil, "Process", process)
    if changed is None:
        validate_native_ready(ready, identities[40], context)
    else:
        with pytest.raises(ValueError, match="redirector"):
            validate_native_ready(ready, identities[40], context)
