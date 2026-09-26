"""Finite launch authority and native readback; no native service mutations."""

from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, NoReturn
from uuid import uuid4

import pytest
from pydantic import JsonValue

from cli.release_transition import journal, native
from cli.release_transition import launcher_linux as linux
from cli.release_transition.request import ReleaseRef, Request
from shared.native_process.ownership import OwnedProcess
from shared.runtime_release import VerifiedRelease


def _constant[T](value: T) -> Callable[..., T]:
    def fixed(*_args: object, **_kwargs: object) -> T:
        return value

    return fixed


def _unexpected(*_args: object, **_kwargs: object) -> NoReturn:
    pytest.fail("unexpected native effect")


@pytest.fixture
def planned(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, JsonValue]:
    tmp_path = tmp_path.resolve()
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    registry = tmp_path / "clusters.json"
    registry.write_text("{}")
    old = ReleaseRef(
        artifact_digest="a" * 64,
        manifest_digest="b" * 64,
        schema_digest="c" * 64,
        source_commit="d" * 40,
    )
    new = old.model_copy(update={"artifact_digest": "e" * 64})
    request = Request(
        id=uuid4(),
        home=str(home),
        registry=str(registry),
        created_at=datetime.now(UTC),
        platform_tag="linux-arm64",
        machine="test",
        previous=old,
        candidate=new,
        executor=new,
        configuration_digest="f" * 64,
    )
    (home / "releases").mkdir(exist_ok=True)
    (home / "releases/current-release").write_text(
        json.dumps(
            {
                "artifact_digest": request.previous.artifact_digest,
                "manifest_digest": request.previous.manifest_digest,
            }
        )
    )
    operation = journal.create(request)
    root = home / "releases" / new.artifact_digest
    (root / "venv/bin").mkdir(parents=True)
    (root / "site").mkdir()
    runtime = VerifiedRelease(
        new.artifact_digest, new.manifest_digest, root, root / "venv/bin/python", root / "site"
    )
    monkeypatch.setattr(linux, "_boot_id", lambda: "test-boot")
    monkeypatch.setattr(linux, "systemd_running", lambda: True)
    monkeypatch.setattr(ReleaseRef, "verify", _constant(runtime))
    plan = linux.plan_launch(request.path, runtime)
    journal.Journal(operation).record_launch(plan)
    return plan


def _properties(plan: dict[str, JsonValue], *, pid: int = 900) -> dict[str, str]:
    launch = linux.LinuxLaunch.model_validate(plan)
    return {
        "Id": launch.unit,
        "LoadState": "loaded",
        "Transient": "yes",
        "Description": launch.description,
        "WorkingDirectory": launch.cwd,
        "User": str(launch.uid),
        "Group": str(launch.gid),
        "Type": "exec",
        "RemainAfterExit": "yes",
        "Restart": "no",
        "KillMode": "control-group",
        "MainPID": str(pid),
        "ControlPID": "0",
        "ControlGroup": launch.cgroup if pid else "",
        "InvocationID": "a" * 32,
        "ExecMainCode": "0" if pid else "1",
        "ExecMainStatus": "0",
        "ActiveState": "active",
        "SubState": "running" if pid else "exited",
        "Result": "success",
    }


def _readback_seams(
    monkeypatch: pytest.MonkeyPatch, plan: dict[str, JsonValue], *, pid: int = 900
) -> None:
    launch = linux.LinuxLaunch.model_validate(plan)
    monkeypatch.setattr(linux, "_properties", _constant(_properties(plan, pid=pid)))

    def property_json(_unit: str, name: str) -> object:
        if name == "ExecStart":
            return [[launch.interpreter, launch.argv, False, 0, 0, 0, 0, pid, 0, 0]]
        assert name == "Environment"
        return [f"{key}={value}" for key, value in launch.environment.items()]

    monkeypatch.setattr(linux, "_property_json", property_json)
    monkeypatch.setattr(linux, "_owner", _constant(OwnedProcess(pid, 1.5, 150)))


def test_plan_launch_writes_its_own_required_adapter_kind(
    planned: dict[str, JsonValue],
) -> None:
    assert planned["kind"] == linux.LINUX
    assert native.recorded_kind(planned) == linux.LINUX
    assert native.for_launch(planned) is linux


def test_launch_requires_exact_record_before_any_native_effect(
    planned: dict[str, JsonValue], monkeypatch: pytest.MonkeyPatch
) -> None:
    path = Path(str(planned["operation"]))
    current = journal.read_operation(path)
    path.write_text(current.model_copy(update={"launch": None}).model_dump_json())
    monkeypatch.setattr(linux, "_command", _unexpected)
    with pytest.raises(ValueError, match="durable intent"):
        linux.launch(planned)


def test_duplicate_launch_refuses_and_replay_reads_same_native_job(
    planned: dict[str, JsonValue], monkeypatch: pytest.MonkeyPatch
) -> None:
    _readback_seams(monkeypatch, planned)
    monkeypatch.setattr(linux, "_command", _unexpected)
    with pytest.raises(RuntimeError, match="already exists"):
        linux.launch(planned)
    job = linux.readback(planned)
    assert job.owner == OwnedProcess(900, 1.5, 150)
    assert job.invocation_id == "a" * 32
    assert not job.finished


def test_missing_replay_never_spawns(
    planned: dict[str, JsonValue], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(linux, "_properties", _constant({"LoadState": "not-found"}))
    monkeypatch.setattr(linux, "_command", _unexpected)
    with pytest.raises(RuntimeError, match="definition"):
        linux.readback(planned)


def test_boot_change_retains_unknown_custody(
    planned: dict[str, JsonValue], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(linux, "_boot_id", lambda: "another-boot")
    with pytest.raises(ValueError, match="this boot"):
        linux.readback(planned)


def test_first_launch_pins_shell_free_command_and_separate_manager_service(
    planned: dict[str, JsonValue], monkeypatch: pytest.MonkeyPatch
) -> None:
    _readback_seams(monkeypatch, planned)
    observations = iter([{"LoadState": "not-found"}, _properties(planned), _properties(planned)])

    def next_properties(_unit: str) -> dict[str, str]:
        return next(observations)

    monkeypatch.setattr(linux, "_properties", next_properties)
    calls: list[tuple[list[str], bool]] = []

    def command(argv: list[str], *, privileged: bool = False) -> subprocess.CompletedProcess[str]:
        operation = journal.read_operation(Path(str(planned["operation"])))
        assert operation.launch == planned and operation.launch_attempted
        calls.append((argv, privileged))
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(linux, "_command", command)
    assert linux.launch(planned).owner is not None
    argv, privileged = calls[0]
    assert privileged
    assert argv[:2] == ["/usr/bin/systemd-run", "--system"]
    assert "--property=KillMode=control-group" in argv
    assert "--expand-environment=no" in argv and "--remain-after-exit" in argv
    assert not {"--scope", "--collect", "--user"}.intersection(argv)
    assert argv[argv.index("--") + 1 :] == planned["argv"]
    assert "cli.release_transition.execute" in argv


def test_crash_after_attempt_never_relaunches_an_absent_unit(
    planned: dict[str, JsonValue], monkeypatch: pytest.MonkeyPatch
) -> None:
    operation = Path(str(planned["operation"]))
    with journal.exclusive(operation) as current:
        current.mark_launch_attempted()
    monkeypatch.setattr(linux, "_command", _unexpected)
    with pytest.raises(RuntimeError, match="already attempted"):
        linux.launch(planned)


def test_changed_native_invocation_cannot_be_adopted(
    planned: dict[str, JsonValue], monkeypatch: pytest.MonkeyPatch
) -> None:
    _readback_seams(monkeypatch, planned)
    first = linux.readback(planned)
    with journal.exclusive(Path(str(planned["operation"]))) as current:
        current.mark_launch_attempted()
        current.record_native(first.identity | {"invocation_id": "b" * 32})
    with pytest.raises(RuntimeError, match="invocation changed"):
        linux.readback(planned)


def test_native_finish_preserves_original_birth_receipt(
    planned: dict[str, JsonValue], monkeypatch: pytest.MonkeyPatch
) -> None:
    _readback_seams(monkeypatch, planned)
    first = linux.readback(planned)
    with journal.exclusive(Path(str(planned["operation"]))) as current:
        current.mark_launch_attempted()
        current.record_native(first.identity)
    _readback_seams(monkeypatch, planned, pid=0)
    assert linux.readback(planned).finished
    assert journal.read_operation(Path(str(planned["operation"]))).native == first.identity


@pytest.mark.parametrize(
    "owner,accepted",
    [
        (OwnedProcess(900, 3601.5, 150), True),
        (OwnedProcess(901, 1.5, 150), False),
        (OwnedProcess(900, 1.5, 151), False),
        (OwnedProcess(900, 1.5, None), False),
    ],
)
def test_readback_uses_kernel_identity_across_wall_clock_changes(
    planned: dict[str, JsonValue],
    monkeypatch: pytest.MonkeyPatch,
    owner: OwnedProcess,
    accepted: bool,
) -> None:
    _readback_seams(monkeypatch, planned)
    first = linux.readback(planned)
    path = Path(str(planned["operation"]))
    with journal.exclusive(path) as current:
        current.mark_launch_attempted()
        current.record_native(first.identity)
    monkeypatch.setattr(linux, "_owner", _constant(owner))
    if accepted:
        assert linux.readback(planned).owner == owner
    else:
        with pytest.raises(RuntimeError, match="birth changed"):
            linux.readback(planned)
    assert journal.read_operation(path).native == first.identity


def test_finished_parent_with_retained_cgroup_children_is_not_finished(
    planned: dict[str, JsonValue], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    launch = linux.LinuxLaunch.model_validate(planned)
    directory = tmp_path / launch.cgroup.lstrip("/")
    directory.mkdir(parents=True)
    (directory / "cgroup.procs").write_text("")
    # Even an empty parent cgroup can have descendants in child cgroups.
    (directory / "cgroup.events").write_text("populated 1\nfrozen 0\n")
    real_path = Path

    def local_path(value: str) -> Path:
        return tmp_path if value == "/sys/fs/cgroup" else real_path(value)

    monkeypatch.setattr(linux, "Path", local_path)
    with pytest.raises(RuntimeError, match="child processes"):
        linux._require_empty_cgroup(launch, launch.cgroup)
    (directory / "cgroup.events").write_text("populated 0\nfrozen 0\n")
    linux._require_empty_cgroup(launch, launch.cgroup)


def test_journal_alias_is_rejected_before_manager_observation(
    planned: dict[str, JsonValue], monkeypatch: pytest.MonkeyPatch
) -> None:
    original = Path(str(planned["operation"]))
    moved = original.with_suffix(".original")
    original.rename(moved)
    original.symlink_to(moved)
    monkeypatch.setattr(linux, "_command", _unexpected)
    with pytest.raises(ValueError):
        linux.launch(planned)


@pytest.mark.parametrize(
    "key,value",
    [
        ("ControlGroup", "/system.slice/ava-boot.home.service"),
        ("Description", "another operation"),
        ("InvocationID", ""),
        ("KillMode", "process"),
    ],
)
def test_readback_rejects_foreign_native_definition(
    planned: dict[str, JsonValue], monkeypatch: pytest.MonkeyPatch, key: str, value: str
) -> None:
    _readback_seams(monkeypatch, planned)
    monkeypatch.setattr(linux, "_properties", _constant(_properties(planned) | {key: value}))
    with pytest.raises(RuntimeError):
        linux.readback(planned)


def test_wrong_exec_argv_is_not_authorized_by_matching_unit_name(
    planned: dict[str, JsonValue], monkeypatch: pytest.MonkeyPatch
) -> None:
    _readback_seams(monkeypatch, planned)
    monkeypatch.setattr(
        linux,
        "_property_json",
        _constant([["/foreign/python", ["/foreign/python"], False, 0, 0, 0, 0, 900, 0, 0]]),
    )
    with pytest.raises(RuntimeError, match="argv"):
        linux.readback(planned)


def test_finished_native_job_is_retained_without_claiming_release_success(
    planned: dict[str, JsonValue], monkeypatch: pytest.MonkeyPatch
) -> None:
    _readback_seams(monkeypatch, planned, pid=0)
    result = linux.readback(planned)
    assert result.finished and result.owner is None
    assert result.exit_code == 1 and result.exit_status == 0
    assert result.active == "active" and result.sub == "exited"


def test_owner_requires_kernel_birth_and_exact_cgroup(
    planned: dict[str, JsonValue], monkeypatch: pytest.MonkeyPatch
) -> None:
    launch = linux.LinuxLaunch.model_validate(planned)
    owner = OwnedProcess(os.getpid(), 1.5, 150)

    class NativeProcess:
        pid = owner.pid

        def ppid(self) -> int:
            return 1

        def uids(self) -> Any:
            return type("Uids", (), {"real": launch.uid})()

        def cwd(self) -> str:
            return launch.cwd

        def cmdline(self) -> list[str]:
            return launch.argv

    monkeypatch.setattr(linux.psutil, "Process", _constant(NativeProcess()))
    monkeypatch.setattr(OwnedProcess, "capture", _constant(owner))
    monkeypatch.setattr(OwnedProcess, "live", _constant(True))
    monkeypatch.setattr(linux, "_cgroup", _constant("/system.slice/ava-boot.foreign.service"))
    with pytest.raises(RuntimeError, match="cgroup custody"):
        linux._owner(launch, owner.pid)
    monkeypatch.setattr(linux, "_cgroup", _constant(launch.cgroup))
    assert linux._owner(launch, owner.pid) == owner


def _closed_attempt(
    planned: dict[str, JsonValue], monkeypatch: pytest.MonkeyPatch, *, captured: bool = True
) -> None:
    _readback_seams(monkeypatch, planned)
    running = linux.readback(planned)
    with journal.exclusive(Path(str(planned["operation"]))) as current:
        current.mark_launch_attempted()
        if captured:
            current.record_native(running.identity)
        current.advance("quiescing")
        current.advance("stopping")
    _readback_seams(monkeypatch, planned, pid=0)


def _retiring_manager(
    planned: dict[str, JsonValue], monkeypatch: pytest.MonkeyPatch
) -> Callable[[list[str]], bool]:
    native_properties = linux._properties
    retired: set[str] = set()

    def properties(unit: str) -> dict[str, str]:
        if unit != planned["unit"] or unit in retired:
            return {"LoadState": "not-found"}
        return native_properties(unit)

    def retire(argv: list[str]) -> bool:
        if argv[:2] != ["/usr/bin/systemctl", "stop"]:
            return False
        operation = journal.read_operation(Path(str(planned["operation"])))
        assert operation.launch == planned and operation.retirement is not None
        assert operation.retirement.state == "requested"
        assert argv[2] == planned["unit"]
        retired.add(argv[2])
        return True

    monkeypatch.setattr(linux, "_properties", properties)
    return retire


@pytest.mark.parametrize("captured", [False, True])
def test_resume_records_closed_attempt_before_new_dispatch_and_keeps_phase(
    planned: dict[str, JsonValue], monkeypatch: pytest.MonkeyPatch, *, captured: bool
) -> None:
    _closed_attempt(planned, monkeypatch, captured=captured)
    path = Path(str(planned["operation"]))
    prior = journal.read_operation(path)
    closed = linux.readback(planned)
    retire = _retiring_manager(planned, monkeypatch)
    calls: list[list[str]] = []

    def command(argv: list[str], *, privileged: bool = False) -> subprocess.CompletedProcess[str]:
        if retire(argv):
            return subprocess.CompletedProcess(argv, 0, "", "")
        current = journal.read_operation(path)
        assert privileged and current.launch is not None and current.launch_attempted
        assert current.attempt == 1 and current.native is None
        assert current.phase == prior.phase and current.direction == prior.direction
        assert current.request == prior.request
        retired = current.retired_executors[0]
        assert retired["launch"] == planned and retired["native"] == prior.native
        assert retired["terminal"] == closed.model_dump(mode="json")
        assert retired["retirement"] == {
            "state": "absent",
            "terminal": closed.model_dump(mode="json"),
        }
        assert current.launch["unit"] != planned["unit"]
        assert current.launch["argv"] == planned["argv"]
        calls.append(argv)
        _readback_seams(monkeypatch, current.launch, pid=901)
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(linux, "_command", command)
    resumed = linux.resume(planned)
    assert resumed.owner == OwnedProcess(901, 1.5, 150)
    assert len(calls) == 1
    # A caller holding the retired plan cannot race or repeat the new attempt.
    monkeypatch.setattr(linux, "_command", _unexpected)
    with pytest.raises(ValueError, match="durable intent"):
        linux.resume(planned)


def test_resume_refuses_live_attempt_without_journal_change(
    planned: dict[str, JsonValue], monkeypatch: pytest.MonkeyPatch
) -> None:
    _readback_seams(monkeypatch, planned)
    path = Path(str(planned["operation"]))
    with journal.exclusive(path) as current:
        current.mark_launch_attempted()
    before = path.read_bytes()
    monkeypatch.setattr(linux, "_command", _unexpected)
    with pytest.raises(RuntimeError, match="not positively closed"):
        linux.resume(planned)
    assert path.read_bytes() == before


@pytest.mark.parametrize("kind", ["missing", "children", "invocation"])
def test_resume_unknown_native_custody_never_changes_journal(
    planned: dict[str, JsonValue], monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    _closed_attempt(planned, monkeypatch)
    if kind == "missing":
        monkeypatch.setattr(linux, "_properties", _constant({"LoadState": "not-found"}))
    elif kind == "invocation":
        monkeypatch.setattr(
            linux,
            "_properties",
            _constant(_properties(planned, pid=0) | {"InvocationID": "b" * 32}),
        )
    else:

        def retained_children(*_args: object) -> NoReturn:
            raise RuntimeError("finished executor still owns native child processes")

        monkeypatch.setattr(linux, "_require_empty_cgroup", retained_children)
    path = Path(str(planned["operation"]))
    before = path.read_bytes()
    monkeypatch.setattr(linux, "_command", _unexpected)
    with pytest.raises(RuntimeError):
        linux.resume(planned)
    assert path.read_bytes() == before


def test_resume_completed_operation_never_observes_or_dispatches_native_job(
    planned: dict[str, JsonValue], monkeypatch: pytest.MonkeyPatch
) -> None:
    _closed_attempt(planned, monkeypatch)
    path = Path(str(planned["operation"]))
    with journal.exclusive(path) as current:
        remaining: tuple[journal.Phase, ...] = (
            "selecting",
            "starting",
            "observing",
            "resuming",
            "complete",
        )
        for phase in remaining:
            current.advance(phase)
    before = path.read_bytes()
    monkeypatch.setattr(linux, "_command", _unexpected)
    monkeypatch.setattr(linux, "_properties", _unexpected)
    with pytest.raises(RuntimeError, match="completed"):
        linux.resume(planned)
    assert path.read_bytes() == before


def test_resume_failed_dispatch_retains_new_attempt_and_previous_evidence(
    planned: dict[str, JsonValue], monkeypatch: pytest.MonkeyPatch
) -> None:
    _closed_attempt(planned, monkeypatch)
    retire = _retiring_manager(planned, monkeypatch)

    def command(argv: list[str], *, privileged: bool = False) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(argv, 0 if retire(argv) else 1, "", "denied")

    monkeypatch.setattr(linux, "_command", command)
    with pytest.raises(RuntimeError, match="unresolved"):
        linux.resume(planned)
    operation = journal.read_operation(Path(str(planned["operation"])))
    assert operation.attempt == 1 and operation.launch_attempted
    assert len(operation.retired_executors) == 1 and operation.launch is not None
    monkeypatch.setattr(linux, "_command", _unexpected)
    with pytest.raises(RuntimeError, match="already attempted"):
        linux.launch(operation.launch)


def test_failed_unit_retirement_records_intent_before_stop_and_reset(
    planned: dict[str, JsonValue], monkeypatch: pytest.MonkeyPatch
) -> None:
    _closed_attempt(planned, monkeypatch)
    closed = linux.readback(planned)
    path = Path(str(planned["operation"]))
    native_properties = linux._properties
    calls: list[str] = []

    def properties(unit: str) -> dict[str, str]:
        return {"LoadState": "not-found"} if "reset-failed" in calls else native_properties(unit)

    def command(argv: list[str], *, privileged: bool = False) -> subprocess.CompletedProcess[str]:
        current = journal.read_operation(path)
        assert current.retirement is not None and current.retirement.state == "requested"
        assert current.retirement.terminal == closed.model_dump(mode="json")
        assert privileged and argv[0] == "/usr/bin/systemctl" and argv[2] == planned["unit"]
        calls.append(argv[1])
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(linux, "_properties", properties)
    monkeypatch.setattr(linux, "_command", command)
    assert linux.retire_current(planned) == closed
    assert calls == ["stop", "reset-failed"]
    current = journal.read_operation(path)
    assert current.retirement is not None and current.retirement.state == "absent"
    before = path.read_bytes()
    monkeypatch.setattr(linux, "_command", _unexpected)
    assert linux.retire_current(planned) == closed
    assert path.read_bytes() == before


@pytest.mark.parametrize(
    ("complete", "absent", "reappeared"),
    [(False, True, False), (True, False, False), (True, True, True)],
)
def test_reboot_never_reopens_unresolved_custody_or_signals_reappeared_executor(
    planned: dict[str, JsonValue],
    monkeypatch: pytest.MonkeyPatch,
    *,
    complete: bool,
    absent: bool,
    reappeared: bool,
) -> None:
    _closed_attempt(planned, monkeypatch)
    path = Path(str(planned["operation"]))
    terminal = linux.readback(planned)
    with journal.exclusive(path) as current:
        if complete:
            phases: tuple[journal.Phase, ...] = (
                "selecting",
                "starting",
                "observing",
                "resuming",
                "complete",
            )
            for phase in phases:
                current.advance(phase)
        current.request_retirement(terminal.model_dump(mode="json"))
        if absent:
            current.record_retired()
    before = path.read_bytes()
    monkeypatch.setattr(linux, "_boot_id", _constant("new-boot"))
    state = "loaded" if reappeared else "not-found"
    monkeypatch.setattr(linux, "_properties", _constant({"LoadState": state}))
    monkeypatch.setattr(linux, "_command", _unexpected)
    pattern = "reappeared" if reappeared else "durable intent"
    with pytest.raises((ValueError, RuntimeError), match=pattern):
        linux.retire_current(planned)
    assert path.read_bytes() == before


@pytest.mark.parametrize("requested", [False, True])
def test_missing_unit_is_retired_only_after_durable_deletion_intent(
    planned: dict[str, JsonValue], monkeypatch: pytest.MonkeyPatch, *, requested: bool
) -> None:
    _closed_attempt(planned, monkeypatch)
    closed = linux.readback(planned)
    path = Path(str(planned["operation"]))
    if requested:
        with journal.exclusive(path) as current:
            current.request_retirement(closed.model_dump(mode="json"))
    before = path.read_bytes()
    monkeypatch.setattr(linux, "_properties", _constant({"LoadState": "not-found"}))
    monkeypatch.setattr(linux, "_command", _unexpected)
    if requested:
        assert linux.retire_current(planned) == closed
        current = journal.read_operation(path)
        assert current.retirement is not None and current.retirement.state == "absent"
    else:
        with pytest.raises(RuntimeError, match="definition"):
            linux.retire_current(planned)
        assert path.read_bytes() == before


@pytest.mark.parametrize("kind", ["live", "invocation", "reappeared", "children"])
def test_retirement_refuses_changed_or_unknown_native_custody(
    planned: dict[str, JsonValue], monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    _closed_attempt(planned, monkeypatch)
    terminal = linux.readback(planned)
    path = Path(str(planned["operation"]))
    with journal.exclusive(path) as current:
        current.request_retirement(terminal.model_dump(mode="json"))
        if kind == "reappeared":
            current.record_retired()
    if kind == "live":
        _readback_seams(monkeypatch, planned)
    elif kind == "invocation":
        monkeypatch.setattr(
            linux,
            "_properties",
            _constant(_properties(planned, pid=0) | {"InvocationID": "b" * 32}),
        )
    elif kind == "children":
        monkeypatch.setattr(linux, "_properties", _constant({"LoadState": "not-found"}))

        def children(*_args: object) -> NoReturn:
            raise RuntimeError("cgroup still has children despite a missing unit")

        monkeypatch.setattr(linux, "_require_empty_cgroup", children)
    before = path.read_bytes()
    monkeypatch.setattr(linux, "_command", _unexpected)
    with pytest.raises(RuntimeError):
        linux.retire_current(planned)
    assert path.read_bytes() == before


def test_retirement_command_failure_preserves_intent_for_next_observation(
    planned: dict[str, JsonValue], monkeypatch: pytest.MonkeyPatch
) -> None:
    _closed_attempt(planned, monkeypatch)
    terminal = linux.readback(planned)
    monkeypatch.setattr(
        linux, "_command", _constant(subprocess.CompletedProcess([], 1, "", "denied"))
    )
    with pytest.raises(RuntimeError, match="retirement unresolved"):
        linux.retire_current(planned)
    current = journal.read_operation(Path(str(planned["operation"])))
    assert current.retirement is not None and current.retirement.state == "requested"
    assert current.retirement.terminal == terminal.model_dump(mode="json")
    assert current.attempt == 0 and current.retired_executors == ()


def test_replaced_invocation_after_stop_cannot_be_reset(
    planned: dict[str, JsonValue], monkeypatch: pytest.MonkeyPatch
) -> None:
    _closed_attempt(planned, monkeypatch)
    calls: list[list[str]] = []

    def command(argv: list[str], *, privileged: bool = False) -> subprocess.CompletedProcess[str]:
        assert privileged and argv == ["/usr/bin/systemctl", "stop", planned["unit"]]
        calls.append(argv)
        monkeypatch.setattr(
            linux,
            "_properties",
            _constant(_properties(planned, pid=0) | {"InvocationID": "b" * 32}),
        )
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(linux, "_command", command)
    with pytest.raises(RuntimeError, match="invocation changed"):
        linux.retire_current(planned)
    assert len(calls) == 1
    current = journal.read_operation(Path(str(planned["operation"])))
    assert current.retirement is not None and current.retirement.state == "requested"
