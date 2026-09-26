"""One finite systemd executor outside the application's home boot cgroup.

The operation journal records the complete launch before systemd is called.
Recovery reads that same named unit; missing evidence never authorizes a second
spawn. This adapter neither supervises application services nor supports macOS.
"""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
from pathlib import Path
from typing import Literal

import psutil
from pydantic import Field, JsonValue, TypeAdapter

from cli.release_transition.journal import Journal, exclusive, read_operation
from cli.release_transition.native import require_private_operation as _private_operation
from cli.release_transition.request import Record
from shared.native_process.ownership import OwnedProcess
from shared.os_boot_unit import systemd_running
from shared.runtime_release import VerifiedRelease

_PROPERTIES = (
    "Id",
    "LoadState",
    "Transient",
    "Description",
    "WorkingDirectory",
    "User",
    "Group",
    "Type",
    "RemainAfterExit",
    "Restart",
    "KillMode",
    "MainPID",
    "ControlPID",
    "ControlGroup",
    "InvocationID",
    "ExecMainCode",
    "ExecMainStatus",
    "ActiveState",
    "SubState",
    "Result",
)


class LinuxLaunch(Record):
    kind: Literal["linux-systemd-v1"] = "linux-systemd-v1"
    operation: str
    attempt: int = Field(ge=0)
    home: str
    registry: str
    unit: str
    boot_id: str
    artifact_digest: str
    manifest_digest: str
    runtime_root: str
    interpreter: str
    cwd: str
    argv: list[str]
    uid: int
    gid: int
    environment: dict[str, str]

    @property
    def cgroup(self) -> str:
        return f"/system.slice/{self.unit}"

    @property
    def description(self) -> str:
        digest = hashlib.sha256(self.model_dump_json().encode()).hexdigest()
        return f"Ava release transition {digest}"


class LinuxJob(Record):
    """Native readback, not a declaration that the release transition succeeded."""

    unit: str
    boot_id: str
    invocation_id: str
    cgroup: str
    owner: OwnedProcess | None
    active: str
    sub: str
    result: str
    exit_code: int
    exit_status: int

    @property
    def finished(self) -> bool:
        return self.owner is None and self.sub in {"exited", "failed", "dead"}

    @property
    def identity(self) -> dict[str, JsonValue]:
        """Persist once; later exit status must not overwrite the captured birth."""
        if self.owner is None:
            raise RuntimeError("an exited executor cannot supply a new native birth receipt")
        return {
            "unit": self.unit,
            "boot_id": self.boot_id,
            "invocation_id": self.invocation_id,
            "cgroup": self.cgroup,
            "pid": self.owner.pid,
            "birth": self.owner.birth,
            "starttime": self.owner.starttime,
        }


def _boot_id() -> str:
    return Path("/proc/sys/kernel/random/boot_id").read_text().strip()


def plan_launch(operation: Path, runtime: VerifiedRelease) -> dict[str, JsonValue]:
    """Build exact journal input; no native job or filesystem mutation."""
    import pwd

    current = read_operation(operation)
    request = current.request
    home = Path(request.home)
    _private_operation(operation, home)
    if (
        runtime.digest != request.executor.artifact_digest
        or runtime.manifest_digest != request.executor.manifest_digest
        or runtime.root != home / "releases" / runtime.digest
        or runtime.root.resolve(strict=True) != runtime.root
        or not runtime.interpreter.is_relative_to(runtime.root)
        or not runtime.cwd.is_relative_to(runtime.root)
    ):
        raise ValueError("executor runtime differs from captured release request")
    registry = Path(request.registry)
    if registry.resolve(strict=True) != registry:
        raise ValueError("release launch registry must be canonical")
    account = pwd.getpwuid(os.getuid())
    launch = LinuxLaunch(
        operation=str(operation),
        attempt=current.attempt,
        home=str(home),
        registry=str(registry),
        unit=(
            f"ava-update.{hashlib.sha256(str(operation).encode()).hexdigest()[:32]}"
            f".a{current.attempt}.service"
        ),
        boot_id=_boot_id(),
        artifact_digest=runtime.digest,
        manifest_digest=runtime.manifest_digest,
        runtime_root=str(runtime.root),
        interpreter=str(runtime.interpreter),
        cwd=str(runtime.cwd),
        argv=list(
            runtime.module_argv("cli.release_transition.execute", "--operation", str(operation))
        ),
        uid=account.pw_uid,
        gid=account.pw_gid,
        environment={
            "HOME": account.pw_dir,
            "AVA_HOME": str(home),
            "AVA_CLUSTER_REGISTRY": str(registry),
            "PATH": f"{runtime.interpreter.parent}:/usr/local/bin:/usr/bin:/bin",
        },
    )
    return launch.model_dump(mode="json")


def _admitted(
    record: dict[str, JsonValue], *, verify: bool, retired_replay: bool = False
) -> LinuxLaunch:
    if not systemd_running():
        raise RuntimeError("finite release launch requires Linux systemd; no platform fallback")
    launch = LinuxLaunch.model_validate(record)
    operation = read_operation(Path(launch.operation))
    request = operation.request
    _private_operation(request.path, Path(request.home))
    settled = (
        retired_replay
        and operation.terminal
        and operation.retirement is not None
        and operation.retirement.state == "absent"
    )
    if operation.launch != record or (launch.boot_id != _boot_id() and not settled):
        raise ValueError("native launch lacks matching durable intent in this boot")
    if verify:
        runtime = request.executor.verify(Path(request.home), request.platform_tag)
    else:
        runtime = VerifiedRelease(
            launch.artifact_digest,
            launch.manifest_digest,
            Path(launch.runtime_root),
            Path(launch.interpreter),
            Path(launch.cwd),
        )
    expected = plan_launch(request.path, runtime)
    if settled:
        # Completed absence is durable history, not custody in the new boot.
        # The retirement reader still refuses any reappeared unit/cgroup and
        # cannot issue native stop/reset from an already settled receipt.
        expected["boot_id"] = launch.boot_id
    if expected != record:
        raise ValueError("native launch differs from captured operation identity")
    return launch


def _command(argv: list[str], *, privileged: bool = False) -> subprocess.CompletedProcess[str]:
    if privileged and os.geteuid() != 0:
        argv = ["sudo", "-n", *argv]
    return subprocess.run(  # noqa: S603 — fixed native tools and admitted immutable argv
        argv, capture_output=True, text=True, check=False, timeout=30
    )


def _properties(unit: str) -> dict[str, str]:
    result = _command(
        [
            "/usr/bin/systemctl",
            "--system",
            "show",
            f"--property={','.join(_PROPERTIES)}",
            unit,
        ]
    )
    values = dict(row.split("=", 1) for row in result.stdout.splitlines() if "=" in row)
    if result.returncode not in {0, 4} or "LoadState" not in values:
        raise RuntimeError(f"cannot read native executor unit: {result.stderr.strip()}")
    return values


def _property_json(unit: str, name: str) -> JsonValue:
    # The unit name alphabet is generated locally; D-Bus escapes punctuation.
    escaped = "".join(char if char.isalnum() else f"_{ord(char):02x}" for char in unit)
    result = _command(
        [
            "/usr/bin/busctl",
            "--system",
            "--json=short",
            "get-property",
            "org.freedesktop.systemd1",
            f"/org/freedesktop/systemd1/unit/{escaped}",
            "org.freedesktop.systemd1.Service",
            name,
        ]
    )
    if result.returncode:
        raise RuntimeError(f"cannot read executor {name}: {result.stderr.strip()}")
    return TypeAdapter(dict[str, JsonValue]).validate_json(result.stdout)["data"]


def _require_definition(launch: LinuxLaunch, properties: dict[str, str]) -> None:
    expected = {
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
        "ControlPID": "0",
    }
    if not set(_PROPERTIES) <= properties.keys() or any(
        properties[key] != value for key, value in expected.items()
    ):
        raise RuntimeError("native executor unit definition differs from journaled launch")
    commands = _property_json(launch.unit, "ExecStart")
    if (
        not isinstance(commands, list)
        or len(commands) != 1
        or not isinstance(commands[0], list)
        or len(commands[0]) != 10
        or commands[0][:3] != [launch.interpreter, launch.argv, False]
    ):
        raise RuntimeError("native executor argv differs from pinned image command")
    environment = TypeAdapter(list[str]).validate_python(
        _property_json(launch.unit, "Environment"), strict=True
    )
    if sorted(environment) != sorted(f"{key}={value}" for key, value in launch.environment.items()):
        raise RuntimeError("native executor environment differs from captured inputs")


def _cgroup(pid: int) -> str:
    for line in (Path("/proc") / str(pid) / "cgroup").read_text().splitlines():
        hierarchy, controllers, path = line.split(":", 2)
        if hierarchy == "0" or "name=systemd" in controllers.split(","):
            return path
    raise RuntimeError("native executor has no observable systemd cgroup")


def _owner(launch: LinuxLaunch, pid: int) -> OwnedProcess:
    process = psutil.Process(pid)
    owner = OwnedProcess.capture(process)
    if (
        owner.starttime is None
        or not owner.live()
        or process.ppid() != 1
        or process.uids().real != launch.uid
        or process.cwd() != launch.cwd
        or process.cmdline() != launch.argv
        or _cgroup(pid) != launch.cgroup
        or not owner.live()
    ):
        raise RuntimeError("executor is outside captured native birth, argv or cgroup custody")
    return owner


def _require_empty_cgroup(launch: LinuxLaunch, observed: str) -> None:
    directory = Path("/sys/fs/cgroup") / launch.cgroup.lstrip("/")
    try:
        members = (directory / "cgroup.procs").read_text().strip()
        events = dict(
            line.split() for line in (directory / "cgroup.events").read_text().splitlines()
        )
    except FileNotFoundError:
        if observed or directory.exists():
            raise RuntimeError("executor cgroup closure is not observable") from None
        return
    if members or events["populated"] != "0":
        raise RuntimeError("finished executor still owns native child processes")


def readback(record: dict[str, JsonValue]) -> LinuxJob:
    """Observe the retained unit; absence/ambiguity refuses instead of respawning."""
    launch = _admitted(record, verify=False)
    before = _properties(launch.unit)
    _require_definition(launch, before)
    if re.fullmatch(r"[0-9a-f]{32}", before["InvocationID"]) is None:
        raise RuntimeError("native executor has no invocation identity")
    pid = int(before["MainPID"])
    cgroup = before["ControlGroup"]
    if cgroup != launch.cgroup and (pid != 0 or cgroup != ""):
        raise RuntimeError("executor manager cgroup differs from journaled unit")
    owner = _owner(launch, pid) if pid else None
    if not pid:
        _require_empty_cgroup(launch, cgroup)
    after = _properties(launch.unit)
    for key in ("MainPID", "InvocationID", "ControlGroup", "ActiveState", "SubState"):
        if before[key] != after[key]:
            raise RuntimeError("executor changed during native observation; retain custody")
    job = LinuxJob(
        unit=launch.unit,
        boot_id=launch.boot_id,
        invocation_id=before["InvocationID"],
        cgroup=launch.cgroup,
        owner=owner,
        active=before["ActiveState"],
        sub=before["SubState"],
        result=before["Result"],
        exit_code=int(before["ExecMainCode"]),
        exit_status=int(before["ExecMainStatus"]),
    )
    _require_captured_identity(Path(launch.operation), job)
    return job


def _require_captured_identity(operation: Path, job: LinuxJob) -> None:
    prior = read_operation(operation).native
    if prior is not None:
        identity = job.model_dump(mode="json")
        for key in ("unit", "boot_id", "invocation_id", "cgroup"):
            if prior[key] != identity[key]:
                raise RuntimeError("native executor invocation changed from captured custody")
        if job.owner is not None:
            captured = TypeAdapter(OwnedProcess).validate_python(
                {key: prior[key] for key in ("pid", "birth", "starttime")}
            )
            if captured.starttime is None or not captured.same_birth(job.owner):
                raise RuntimeError("native executor birth changed from captured custody")


def _launch_command(planned: LinuxLaunch) -> list[str]:
    command = [
        "/usr/bin/systemd-run",
        "--system",
        "--no-ask-password",
        "--expand-environment=no",
        f"--unit={planned.unit}",
        f"--description={planned.description}",
        "--service-type=exec",
        "--remain-after-exit",
        "--property=Restart=no",
        "--property=KillMode=control-group",
        f"--uid={planned.uid}",
        f"--gid={planned.gid}",
        f"--working-directory={planned.cwd}",
    ]
    command.extend(f"--setenv={key}={value}" for key, value in planned.environment.items())
    return [*command, "--", *planned.argv]


def _dispatch(journal: Journal, planned: LinuxLaunch) -> None:
    """The caller holds the operation lock across admission and native submission."""
    if journal.operation.launch_attempted:
        raise RuntimeError("executor launch was already attempted; recover only by readback")
    if _properties(planned.unit)["LoadState"] != "not-found":
        raise RuntimeError(
            "executor unit already exists; recover by readback, never duplicate launch"
        )
    journal.mark_launch_attempted()
    result = _command(_launch_command(planned), privileged=True)
    if result.returncode:
        raise RuntimeError(
            f"native launch unresolved; retain intent and read back {planned.unit}: {result.stderr.strip()}"
        )


def launch(record: dict[str, JsonValue]) -> LinuxJob:
    """Submit once, only after the exact plan and attempt are durable."""
    parsed = LinuxLaunch.model_validate(record)
    with exclusive(Path(parsed.operation)) as journal:
        _dispatch(journal, _admitted(record, verify=True))
    # Release promptly: Type=exec acknowledges execve, while the executor needs
    # this same journal lock before it may change the application generation.
    return readback(record)


def _retirement_absent(launch: LinuxLaunch) -> bool:
    if _properties(launch.unit)["LoadState"] != "not-found":
        return False
    _require_empty_cgroup(launch, "")
    return True


def _same_closed_invocation(launch: LinuxLaunch, terminal: LinuxJob) -> None:
    fresh = readback(launch.model_dump(mode="json"))
    if not fresh.finished or any(
        getattr(fresh, key) != getattr(terminal, key)
        for key in ("unit", "boot_id", "invocation_id", "cgroup")
    ):
        raise RuntimeError("executor changed after closure; retain unresolved retirement")


def _retire(journal: Journal, launch: LinuxLaunch) -> LinuxJob:
    """The lock owner records deletion intent before removing a closed native unit."""
    receipt = journal.operation.retirement
    if receipt is None:
        terminal = readback(launch.model_dump(mode="json"))
        if not terminal.finished:
            raise RuntimeError("executor is not positively closed; retain current native custody")
        journal.request_retirement(terminal.model_dump(mode="json"))
    else:
        terminal = LinuxJob.model_validate(receipt.terminal)
    if not _retirement_absent(launch):
        if receipt is not None and receipt.state == "absent":
            raise RuntimeError("retired executor unit reappeared; native custody is unknown")
        _same_closed_invocation(launch, terminal)
        _command(["/usr/bin/systemctl", "stop", launch.unit], privileged=True)
        if not _retirement_absent(launch):
            # Failed units retain failure state after stop. Recheck the exact
            # invocation before asking the manager to release that definition.
            _same_closed_invocation(launch, terminal)
            result = _command(["/usr/bin/systemctl", "reset-failed", launch.unit], privileged=True)
            if not _retirement_absent(launch):
                raise RuntimeError(
                    f"native executor retirement unresolved: {result.stderr.strip()}"
                )
    journal.record_retired()
    return terminal


def retire_current(record: dict[str, JsonValue]) -> LinuxJob:
    """A living caller retires a closed executor; an executor cannot retire itself."""
    parsed = LinuxLaunch.model_validate(record)
    with exclusive(Path(parsed.operation)) as journal:
        return _retire(journal, _admitted(record, verify=False, retired_replay=True))


def resume(record: dict[str, JsonValue]) -> LinuxJob:
    """Continue the same decision only after the previous native attempt closed."""
    parsed = LinuxLaunch.model_validate(record)
    path = Path(parsed.operation)
    with exclusive(path) as journal:
        launch = _admitted(record, verify=False)
        if journal.operation.terminal:
            raise RuntimeError("a completed release operation cannot launch another executor")
        terminal = _retire(journal, launch)
        runtime = journal.operation.request.executor.verify(
            Path(parsed.home), journal.operation.request.platform_tag
        )
        journal.relaunch(terminal.model_dump(mode="json"))
        replacement = plan_launch(path, runtime)
        journal.record_launch(replacement)
        _dispatch(journal, LinuxLaunch.model_validate(replacement))
    return readback(replacement)
