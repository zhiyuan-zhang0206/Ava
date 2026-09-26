"""One finite launchd job per release attempt, running the signed helper's finite mode.

The persistent home helper keeps owning ava-root; this job owns only the retained
executor and the finite tools that stay in the job's process group. launchd's
documented cleanup covers that group, not descendants that create their own
group or session, so escape observed while it is still traceable refuses and a
terminal job or empty group never certifies closure beyond that scope.

The operation journal records the complete launch before launchd is called.
Recovery reads that same labelled job; missing evidence never authorizes another
bootstrap. This adapter makes no release decision and never kickstarts a job.
"""

from __future__ import annotations

import hashlib
import os
import plistlib
import subprocess
import time
from pathlib import Path
from typing import Literal

import psutil
from pydantic import Field, JsonValue

from cli.release_transition.journal import Journal, exclusive, read_operation
from cli.release_transition.launchd_print import (
    SUPPORTED_PRODUCT_MAJORS,
    LaunchdJob,
    read_job,
)
from cli.release_transition.native import DARWIN, require_private_operation
from cli.release_transition.request import PitrRequest, Record, Request
from services.permissions_helper import finite_artifact
from services.permissions_helper.finite_artifact import HelperArtifact
from shared.native_process import native_boot_id
from shared.native_process.ownership import OwnedProcess, capture_tree
from shared.proc import run_bounded
from shared.runtime_release import VerifiedRelease
from shared.verified_file import regular_bytes

LAUNCHCTL = "/bin/launchctl"
EXIT_TIMEOUT_S = 20
UMASK = 0o022
# Loaded-job policy measured for the rendered plist; any drift (for example
# "abandon process group" or a keepalive property) refuses as custody change.
POLICY = ("runatload", "inferred program")
# EXIT SYNC: services/permissions_helper/helper/main.swift::FiniteExit.
FINITE_EXIT = {
    0: "executor-succeeded",
    64: "usage",
    65: "not-job-leader",
    71: "spawn-failed",
    75: "interrupted-before-spawn",
    80: "executor-failed",
    81: "executor-signaled",
    82: "custody-lost",
}
_QUERY_TIMEOUT_S = 30.0
_SETTLE_S = 30.0
_CLOSURE_WAIT_S = 5.0
_POLL_S = 0.05


class Birth(Record):
    """One captured native process birth (psutil monotonic start on macOS)."""

    pid: int = Field(gt=1)
    birth: float
    starttime: int | None

    @classmethod
    def of(cls, process: OwnedProcess) -> Birth:
        return cls(pid=process.pid, birth=process.birth, starttime=process.starttime)

    def owned(self) -> OwnedProcess:
        return OwnedProcess(self.pid, self.birth, self.starttime)


class DarwinLaunch(Record):
    kind: Literal["darwin-launchd-v1"] = DARWIN
    operation: str
    attempt: int = Field(ge=0)
    home: str
    registry: str
    label: str
    domain: str
    uid: int = Field(ge=0)
    boot_id: str
    macos_product: str
    macos_build: str
    plist: str
    plist_sha256: str
    stdout: str
    stderr: str
    helper: HelperArtifact
    artifact_digest: str
    manifest_digest: str
    runtime_root: str
    interpreter: str
    cwd: str
    argv: list[str]
    environment: dict[str, str]
    exit_timeout: int = Field(gt=0)

    @property
    def target(self) -> str:
        return f"{self.domain}/{self.label}"

    def program_arguments(self) -> list[str]:
        """Fixed helper argv; the helper builds the executor environment from it alone."""
        pairs = [
            part
            for key in sorted(self.environment)
            for part in ("--env", f"{key}={self.environment[key]}")
        ]
        return [
            self.helper.executable,
            "--finite-executor",
            "v1",
            "--cwd",
            self.cwd,
            *pairs,
            "--",
            *self.argv,
        ]


class NativeReceipt(Record):
    """Recorded by the executor before any effect: attempt, helper and executor births."""

    kind: Literal["darwin-launchd-v1"] = DARWIN
    label: str
    domain: str
    boot_id: str
    asid: int
    pgid: int = Field(gt=1)
    helper: Birth
    executor: Birth


class DarwinJob(Record):
    """Native readback, not a declaration that the release transition succeeded."""

    kind: Literal["darwin-launchd-v1"] = DARWIN
    label: str
    domain: str
    boot_id: str
    asid: int
    state: Literal["running", "not running"]
    runs: int
    helper: Birth | None
    executor: Birth | None
    pgid: int | None
    exit_code: int | None
    signal: int | None
    closed: dict[str, Birth] | None

    @property
    def finished(self) -> bool:
        return self.state == "not running" and self.helper is None

    @property
    def identity(self) -> dict[str, JsonValue]:
        """Helper and executor births stay distinct; neither substitutes for the other."""
        if self.helper is None or self.executor is None or self.pgid is None:
            raise RuntimeError("only a running executor can supply a native birth receipt")
        return NativeReceipt(
            label=self.label,
            domain=self.domain,
            boot_id=self.boot_id,
            asid=self.asid,
            pgid=self.pgid,
            helper=self.helper,
            executor=self.executor,
        ).model_dump(mode="json")


def plist_document(launch: DarwinLaunch) -> bytes:
    """The exact private job definition; it is never placed in LaunchAgents."""
    document = {
        "Label": launch.label,
        "ProgramArguments": launch.program_arguments(),
        "WorkingDirectory": launch.cwd,
        "RunAtLoad": True,
        "KeepAlive": False,
        "AbandonProcessGroup": False,
        "ExitTimeOut": launch.exit_timeout,
        "ProcessType": "Standard",
        "Umask": UMASK,
        "StandardOutPath": launch.stdout,
        "StandardErrorPath": launch.stderr,
    }
    return plistlib.dumps(document, sort_keys=True)


def _boot_id() -> str:
    boot = native_boot_id()
    if boot is None:
        raise RuntimeError("macOS release launch requires a native boot identity")
    return boot


def _macos() -> tuple[str, str]:
    try:
        result = run_bounded(
            ["/usr/sbin/sysctl", "-n", "kern.osproductversion", "kern.osversion"],
            timeout=10,
            capture_output=True,
            text=True,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("cannot read the macOS product and build") from exc
    lines = result.stdout.split()
    if result.returncode or len(lines) != 2:
        raise RuntimeError("cannot read the macOS product and build")
    return lines[0], lines[1]


def _supported_macos() -> tuple[str, str]:
    product, build = _macos()
    if product.split(".")[0] not in SUPPORTED_PRODUCT_MAJORS:
        raise RuntimeError(f"macOS {product} ({build}) lacks a verified launchd readback contract")
    return product, build


def _sha256(path: Path) -> str:
    return finite_artifact.executable_sha256(path)


def admit_request(request: Request | PitrRequest) -> None:
    """Typed macOS scope, checked before any journal reservation or effect."""
    if isinstance(request, PitrRequest):
        raise ValueError(  # noqa: TRY004 — typed scope refusal on the CLI refusal path
            "PITR is not admitted on macOS: its data-plane owners create process groups "
            "outside launchd job cleanup"
        )
    raise RuntimeError(
        "macOS release start through the persistent home helper is not connected; "
        "the finite executor adapter refuses before quiescing"
    )


def _require_launch_text(values: list[str]) -> None:
    # launchctl prints one argument per line; a control character or edge
    # whitespace could not be compared exactly against the loaded definition.
    if any(
        not value or value != value.strip() or any(ord(c) < 32 for c in value) for value in values
    ):
        raise ValueError("launch inputs must be printable without edge whitespace")


def _plan(
    operation: Path, runtime: VerifiedRelease, helper: HelperArtifact
) -> dict[str, JsonValue]:
    """Build exact journal input; no native job or filesystem mutation."""
    import pwd

    current = read_operation(operation)
    request = current.request
    if isinstance(request, PitrRequest):
        raise ValueError("PITR is not admitted by the macOS finite executor")  # noqa: TRY004 — typed scope refusal
    home = Path(request.home)
    require_private_operation(operation, home)
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
    product, build = _supported_macos()
    label = (
        f"com.ava.release-executor.{hashlib.sha256(str(operation).encode()).hexdigest()[:32]}"
        f".a{current.attempt}"
    )
    directory = operation.parent / "executor" / f"a{current.attempt}"
    fields: dict[str, object] = {
        "operation": str(operation),
        "attempt": current.attempt,
        "home": str(home),
        "registry": str(registry),
        "label": label,
        "domain": f"gui/{account.pw_uid}",
        "uid": account.pw_uid,
        "boot_id": _boot_id(),
        "macos_product": product,
        "macos_build": build,
        "plist": str(directory / f"{label}.plist"),
        "plist_sha256": "0" * 64,
        "stdout": str(directory / "stdout.log"),
        "stderr": str(directory / "stderr.log"),
        "helper": helper,
        "artifact_digest": runtime.digest,
        "manifest_digest": runtime.manifest_digest,
        "runtime_root": str(runtime.root),
        "interpreter": str(runtime.interpreter),
        "cwd": str(runtime.cwd),
        "argv": list(
            runtime.module_argv("cli.release_transition.execute", "--operation", str(operation))
        ),
        "environment": {
            "HOME": account.pw_dir,
            "AVA_HOME": str(home),
            "AVA_CLUSTER_REGISTRY": str(registry),
            "PATH": f"{runtime.interpreter.parent}:/usr/bin:/bin:/usr/sbin:/sbin",
        },
        "exit_timeout": EXIT_TIMEOUT_S,
    }
    draft = DarwinLaunch.model_validate(fields)
    _require_launch_text([*draft.program_arguments(), draft.plist, draft.stdout, draft.stderr])
    digest = hashlib.sha256(plist_document(draft)).hexdigest()
    return draft.model_copy(update={"plist_sha256": digest}).model_dump(mode="json")


def plan_launch(operation: Path, runtime: VerifiedRelease) -> dict[str, JsonValue]:
    """Plan the next attempt with the verified signed helper of every earlier attempt."""
    helper = finite_artifact.capture()
    retired = read_operation(operation).retired_executors
    if retired and DarwinLaunch.model_validate(retired[-1]["launch"]).helper != helper:
        raise RuntimeError(
            "signed helper changed between executor attempts; upgrade it only outside an operation"
        )
    return _plan(operation, runtime, helper)


def _admitted(
    record: dict[str, JsonValue], *, verify: bool, retired_replay: bool = False
) -> DarwinLaunch:
    launch = DarwinLaunch.model_validate(record)
    operation = read_operation(Path(launch.operation))
    request = operation.request
    require_private_operation(request.path, Path(request.home))
    settled = (
        retired_replay
        and operation.terminal
        and operation.retirement is not None
        and operation.retirement.state == "absent"
    )
    if operation.launch != record or (launch.boot_id != _boot_id() and not settled):
        raise ValueError("native launch lacks matching durable intent in this boot")
    if settled:
        # Completed absence is durable history, not custody in a later boot or
        # OS build. Retirement only re-proves exact label absence.
        return launch
    if verify:
        runtime = request.executor.verify(Path(request.home), request.platform_tag)
        expected = plan_launch(request.path, runtime)
    else:
        runtime = VerifiedRelease(
            launch.artifact_digest,
            launch.manifest_digest,
            Path(launch.runtime_root),
            Path(launch.interpreter),
            Path(launch.cwd),
        )
        expected = _plan(request.path, runtime, launch.helper)
    if expected != record:
        raise ValueError("native launch differs from captured operation identity")
    return launch


def _query(launch: DarwinLaunch) -> str | None:
    """Absence is one exact launchd answer; every other failure is unknown."""
    try:
        result = run_bounded(
            [LAUNCHCTL, "print", launch.target],
            timeout=_QUERY_TIMEOUT_S,
            capture_output=True,
            text=True,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("launchd query timed out; native custody retained") from exc
    absent = (
        f'Bad request.\nCould not find service "{launch.label}" in domain for user gui: '
        f"{launch.uid}\n"
    )
    if result.returncode == 113 and result.stdout == "" and result.stderr == absent:
        return None
    if result.returncode:
        raise RuntimeError(f"cannot read native executor job: {result.stderr.strip()!r}")
    return result.stdout


def _job(launch: DarwinLaunch) -> LaunchdJob:
    text = _query(launch)
    if text is None:
        raise RuntimeError("native executor job is absent before retirement; custody retained")
    job = read_job(text, launch.target)
    expected = (
        launch.plist,
        launch.helper.executable,
        tuple(launch.program_arguments()),
        launch.cwd,
        launch.stdout,
        launch.stderr,
        launch.uid,
        f"{UMASK:o}",
        launch.exit_timeout,
        1,
        POLICY,
    )
    observed = (
        job.path,
        job.program,
        job.arguments,
        job.working_directory,
        job.stdout_path,
        job.stderr_path,
        job.uid,
        job.umask,
        job.exit_timeout,
        job.runs,
        job.properties,
    )
    if observed != expected:
        raise RuntimeError("loaded executor job differs from its journaled launch")
    if _sha256(Path(launch.plist)) != launch.plist_sha256:
        raise RuntimeError("retained executor job definition changed")
    return job


def _process_group(pid: int) -> int:
    return os.getpgid(pid)


def _helper(launch: DarwinLaunch, pid: int) -> OwnedProcess:
    process = psutil.Process(pid)
    helper = OwnedProcess.capture(process)
    if (
        process.ppid() != 1
        or _process_group(pid) != pid
        or process.uids().real != launch.uid
        or process.cmdline() != launch.program_arguments()
        or Path(process.exe()) != Path(launch.helper.executable)
        or _sha256(Path(launch.helper.executable)) != launch.helper.sha256
        or not helper.live()
    ):
        raise RuntimeError("finite helper is outside its captured signed launch identity")
    return helper


def _executor(launch: DarwinLaunch, helper: OwnedProcess) -> OwnedProcess | None:
    children = psutil.Process(helper.pid).children(recursive=False)
    if len(children) > 1:
        raise RuntimeError("finite helper owns more than its one executor")
    if not children:
        return None
    process = psutil.Process(children[0].pid)
    executor = OwnedProcess.capture(process)
    if (
        process.ppid() != helper.pid
        or _process_group(executor.pid) != helper.pid
        or process.uids().real != launch.uid
        or process.cmdline() != launch.argv
        or process.cwd() != launch.cwd
        or not executor.live()
    ):
        raise RuntimeError("executor is outside the helper's direct-child and job-group custody")
    return executor


def _require_group_tree(helper: OwnedProcess) -> None:
    """Every traceable descendant must stay where launchd's cleanup reaches."""
    for member in capture_tree(helper):
        try:
            group = _process_group(member.pid)
        except ProcessLookupError:
            continue
        if group != helper.pid and member.live():
            raise RuntimeError(
                "a job descendant left the launchd job process group; custody unresolved"
            )


def _group_alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _receipt(launch: DarwinLaunch) -> NativeReceipt | None:
    native = read_operation(Path(launch.operation)).native
    return None if native is None else NativeReceipt.model_validate(native)


def _closed(launch: DarwinLaunch) -> dict[str, Birth] | None:
    """Recorded births closed and the job group empty; nothing about escaped groups."""
    receipt = _receipt(launch)
    if receipt is None:
        # The executor records both births before any effect or child, so a
        # job that ended first only ever held the helper and the executor.
        return None
    births = {"helper": receipt.helper, "executor": receipt.executor}
    deadline = time.monotonic() + _CLOSURE_WAIT_S
    while any(birth.owned().live() for birth in births.values()) or _group_alive(receipt.pgid):
        if time.monotonic() >= deadline:
            raise RuntimeError(
                "terminal executor job still has live group members; custody retained"
            )
        time.sleep(_POLL_S)
    return births


def _running(launch: DarwinLaunch, pid: int) -> tuple[OwnedProcess, OwnedProcess | None]:
    """A process that exits mid-observation is changed custody, never absence."""
    try:
        helper = _helper(launch, pid)
        executor = _executor(launch, helper)
        _require_group_tree(helper)
    except (psutil.NoSuchProcess, ProcessLookupError) as exc:
        raise RuntimeError(
            "executor job changed during native observation; retain custody"
        ) from exc
    except psutil.AccessDenied as exc:
        raise RuntimeError("executor job is not observable; retain custody") from exc
    return helper, executor


def _observe(launch: DarwinLaunch) -> DarwinJob:
    first = _job(launch)
    helper = executor = None
    closed = None
    if first.state == "running":
        if first.pid is None:
            raise RuntimeError("running executor job has no native owner")
        helper, executor = _running(launch, first.pid)
    else:
        closed = _closed(launch)
    second = _job(launch)
    facts = ("state", "pid", "runs", "exit_code", "signal", "asid")
    if any(getattr(first, key) != getattr(second, key) for key in facts):
        raise RuntimeError("executor job changed during native observation; retain custody")
    if helper is not None and not helper.live():
        raise RuntimeError("finite helper changed during native observation; retain custody")
    job = DarwinJob(
        label=launch.label,
        domain=launch.domain,
        boot_id=launch.boot_id,
        asid=first.asid,
        state=first.state,
        runs=first.runs,
        helper=None if helper is None else Birth.of(helper),
        executor=None if executor is None else Birth.of(executor),
        pgid=None if helper is None else helper.pid,
        exit_code=first.exit_code,
        signal=first.signal,
        closed=closed,
    )
    _require_captured_identity(launch, job)
    return job


def _require_captured_identity(launch: DarwinLaunch, job: DarwinJob) -> None:
    prior = _receipt(launch)
    if prior is None:
        return
    if (prior.label, prior.domain, prior.boot_id, prior.asid) != (
        job.label,
        job.domain,
        job.boot_id,
        job.asid,
    ):
        raise RuntimeError("native executor job changed from captured custody")
    if job.helper is not None and (prior.helper != job.helper or prior.pgid != job.pgid):
        raise RuntimeError("finite helper birth changed from captured custody")
    if job.executor is not None and prior.executor != job.executor:
        raise RuntimeError("native executor birth changed from captured custody")


def readback(record: dict[str, JsonValue]) -> DarwinJob:
    """Observe the retained job; absence or ambiguity refuses instead of respawning."""
    return _observe(_admitted(record, verify=False))


def executor_receipt(record: dict[str, JsonValue]) -> dict[str, JsonValue]:
    """The calling process must be the helper's recorded direct child in the job group."""
    job = readback(record)
    if job.executor is None or job.executor.pid != os.getpid() or not job.executor.owned().live():
        raise RuntimeError("this process is not the recorded external executor")
    return job.identity


def _write_plist(launch: DarwinLaunch) -> None:
    from shared.private_storage import ensure_private_dir

    path = Path(launch.plist)
    ensure_private_dir(path.parent.parent)
    ensure_private_dir(path.parent)
    document = plist_document(launch)
    if hashlib.sha256(document).hexdigest() != launch.plist_sha256:
        raise ValueError("rendered executor job differs from its journaled digest")
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    except FileExistsError:
        # An interrupted launch may have written the definition before its
        # durable dispatch record; only identical bytes can be reused.
        if regular_bytes(path) != document:
            raise RuntimeError(
                "an unrelated executor job definition occupies this attempt"
            ) from None
        return
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(document)
        stream.flush()
        os.fsync(stream.fileno())


def _command(argv: list[str]) -> subprocess.CompletedProcess[str]:
    try:
        return run_bounded(argv, timeout=_QUERY_TIMEOUT_S, capture_output=True, text=True)
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"launchd command outcome unknown: {argv[1]}") from exc


def _dispatch(journal: Journal, planned: DarwinLaunch) -> None:
    """The caller holds the operation lock across admission and bootstrap."""
    if journal.operation.launch_attempted:
        raise RuntimeError("executor launch was already attempted; recover only by readback")
    if _query(planned) is not None:
        raise RuntimeError("executor job already exists; recover by readback, never duplicate")
    _write_plist(planned)
    journal.mark_launch_attempted()
    result = _command([LAUNCHCTL, "bootstrap", planned.domain, planned.plist])
    if result.returncode:
        raise RuntimeError(
            f"native launch unresolved; retain intent and read back {planned.label}: "
            f"{result.stderr.strip()}"
        )


def _settle(record: dict[str, JsonValue]) -> DarwinJob:
    """Bounded observation after bootstrap; RunAtLoad starts the helper asynchronously."""
    deadline = time.monotonic() + _SETTLE_S
    while True:
        try:
            job = readback(record)
        except RuntimeError as exc:
            if time.monotonic() >= deadline:
                raise RuntimeError(
                    "native launch is not yet observable; retain intent and read back"
                ) from exc
        else:
            if job.finished or job.executor is not None or time.monotonic() >= deadline:
                return job
        time.sleep(0.1)


def launch(record: dict[str, JsonValue]) -> DarwinJob:
    """Bootstrap once, only after the exact plan and attempt are durable."""
    parsed = DarwinLaunch.model_validate(record)
    with exclusive(Path(parsed.operation)) as journal:
        _dispatch(journal, _admitted(record, verify=True))
    # Release promptly: the executor needs this same journal lock to record
    # its native births before it may change the application generation.
    return _settle(record)


def _await_absent(launch: DarwinLaunch, detail: str) -> None:
    deadline = time.monotonic() + _QUERY_TIMEOUT_S
    while _query(launch) is not None:
        if time.monotonic() >= deadline:
            raise RuntimeError(f"native executor retirement unresolved: {detail}")
        time.sleep(_POLL_S)


def _retire(journal: Journal, launch: DarwinLaunch) -> DarwinJob:
    """The lock owner records deletion intent before removing a closed native job."""
    receipt = journal.operation.retirement
    if receipt is None:
        terminal = _observe(launch)
        if not terminal.finished:
            raise RuntimeError("executor is not positively closed; retain current native custody")
        journal.request_retirement(terminal.model_dump(mode="json"))
    else:
        terminal = DarwinJob.model_validate(receipt.terminal)
    if _query(launch) is not None:
        if receipt is not None and receipt.state == "absent":
            raise RuntimeError("retired executor job reappeared; native custody is unknown")
        if _observe(launch) != terminal:
            raise RuntimeError("executor changed after closure; retain unresolved retirement")
        result = _command([LAUNCHCTL, "bootout", launch.target])
        _await_absent(launch, result.stderr.strip())
    journal.record_retired()
    return terminal


def retire_current(record: dict[str, JsonValue]) -> DarwinJob:
    """A living caller retires a closed executor; an executor cannot retire itself."""
    parsed = DarwinLaunch.model_validate(record)
    with exclusive(Path(parsed.operation)) as journal:
        return _retire(journal, _admitted(record, verify=False, retired_replay=True))


def resume(record: dict[str, JsonValue]) -> DarwinJob:
    """Continue the same decision only after the previous native attempt closed."""
    parsed = DarwinLaunch.model_validate(record)
    path = Path(parsed.operation)
    with exclusive(path) as journal:
        current = _admitted(record, verify=False)
        if journal.operation.terminal:
            raise RuntimeError("a completed release operation cannot launch another executor")
        terminal = _retire(journal, current)
        request = journal.operation.request
        runtime = request.executor.verify(Path(parsed.home), request.platform_tag)
        if finite_artifact.capture() != current.helper:
            raise RuntimeError(
                "signed helper changed between executor attempts; upgrade it only outside "
                "an operation"
            )
        journal.relaunch(terminal.model_dump(mode="json"))
        replacement = plan_launch(path, runtime)
        journal.record_launch(replacement)
        _dispatch(journal, DarwinLaunch.model_validate(replacement))
    return _settle(replacement)
