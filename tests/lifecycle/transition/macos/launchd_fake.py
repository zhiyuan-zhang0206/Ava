"""A fake ``launchctl`` for macOS adapter unit tests; no native job is touched.

It renders ``print`` output in the measured macOS 26 structure so every adapter
path goes through the real fail-closed parser and definition comparison.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from pydantic import JsonValue

from cli.release_transition import journal, launchd_custody
from cli.release_transition import launcher_macos as macos
from cli.release_transition.launchd_print import _DARWIN_STRSIGNAL
from cli.release_transition.request import ReleaseRef, Request
from services.permissions_helper import finite_artifact
from services.permissions_helper.finite_artifact import HelperArtifact
from shared.native_process.ownership import OwnedProcess
from shared.runtime_release import VerifiedRelease

HELPER = HelperArtifact(
    app="/fixture/AvaPermissionsHelper.app",
    executable="/fixture/AvaPermissionsHelper.app/Contents/MacOS/AvaPermissionsHelper",
    sha256="a" * 64,
    requirement='identifier "com.ava.permissions-helper" and certificate leaf = H"00"',
)
HELPER_BIRTH = OwnedProcess(900, 1.5, None)
EXECUTOR_BIRTH = OwnedProcess(901, 2.5, None)
ASID = 100023


def domain_print(uid: int, asid: int) -> str:
    """The measured login-domain header shape; the services list is elided."""
    return (
        f"gui/{uid} = {{\n\ttype = login\n\thandle = {asid}\n\tactive count = 474\n"
        "\tsession = Aqua\n\tsecurity context = {\n"
        f"\t\tuid = {uid}\n\t\tasid = {asid}\n\t}}\n\n\tservices = {{\n"
        "\t\t    3621      - \tapplication.fixture\n\t}\n}\n"
    )


def write_group_receipt(
    launch: macos.DarwinLaunch, helper_pid: int, asid: int = ASID, *, pgid: int | None = None
) -> None:
    """What the finite helper publishes before its only spawn."""
    path = Path(launch.group_receipt)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.unlink(missing_ok=True)
    path.write_text(
        json.dumps(
            {
                "asid": asid,
                "finite_executor": "v1",
                "helper_pid": helper_pid,
                "pgid": helper_pid if pgid is None else pgid,
            }
        )
    )
    path.chmod(0o600)


def render(
    launch: macos.DarwinLaunch,
    *,
    state: str = "running",
    pid: int = HELPER_BIRTH.pid,
    exit_code: int | None = None,
    signal: int | None = None,
    runs: int = 1,
    asid: int = ASID,
    properties: tuple[str, ...] = macos.POLICY,
    arguments: list[str] | None = None,
) -> str:
    running = state == "running"
    if running:
        outcome = [f"\tpid = {pid}", "\tlast exit code = (never exited)"]
    elif signal is not None:
        outcome = [f"\tlast terminating signal = {_DARWIN_STRSIGNAL[signal]}: {signal}"]
    else:
        outcome = [f"\tlast exit code = {exit_code}"]
    lines = [
        f"{launch.target} = {{",
        f"\tactive count = {1 if running else 0}",
        f"\tpath = {launch.plist}",
        "\ttype = LaunchAgent",
        f"\tstate = {state}",
        "",
        f"\tprogram = {launch.helper.executable}",
        "\targuments = {",
        *(f"\t\t{part}" for part in (arguments or launch.program_arguments())),
        "\t}",
        "",
        f"\tworking directory = {launch.cwd}",
        "",
        f"\tstdout path = {launch.stdout}",
        f"\tstderr path = {launch.stderr}",
        "\tinherited environment = {",
        "\t\tSSH_AUTH_SOCK => /var/run/com.apple.launchd.fixture/Listeners",
        "\t}",
        "",
        "\tenvironment = {",
        f"\t\tXPC_SERVICE_NAME => {launch.label}",
        "\t}",
        "",
        f"\tdomain = gui/{launch.uid} [{asid}]",
        "\tumask = 22",
        f"\tasid = {asid}",
        "\tminimum runtime = 10",
        f"\texit timeout = {launch.exit_timeout}",
        f"\truns = {runs}",
        *outcome,
        "",
        "\tresource coalition = {",
        "\t\tID = 1",
        "\t\ttype = resource",
        "\t\tstate = active",
        "\t\tactive count = 1",
        f"\t\tname = {launch.label}",
        "\t}",
        "",
        f"\tproperties = {' | '.join(properties)}",
        "}",
    ]
    return "\n".join(lines) + "\n"


class FakeLaunchd:
    """Records every mutating launchctl call; queries answer from `jobs`."""

    def __init__(self) -> None:
        self.jobs: dict[str, str] = {}
        self.commands: list[list[str]] = []
        self.on_command: Callable[[list[str]], subprocess.CompletedProcess[str] | None] | None = (
            None
        )
        self.print_error: subprocess.CompletedProcess[str] | None = None
        self.domain_asid = ASID

    def __call__(self, argv: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        if argv[0] == "/usr/sbin/sysctl":
            return subprocess.CompletedProcess(argv, 0, "26.6.2\n25G83\n", "")
        assert argv[0] == macos.LAUNCHCTL
        if argv[1] == "print":
            if self.print_error is not None:
                return self.print_error
            target = argv[2]
            if target.count("/") == 1:
                uid = int(target.removeprefix("gui/"))
                return subprocess.CompletedProcess(argv, 0, domain_print(uid, self.domain_asid), "")
            if target in self.jobs:
                return subprocess.CompletedProcess(argv, 0, self.jobs[target], "")
            domain, label = target.rsplit("/", 1)
            uid = domain.removeprefix("gui/")
            stderr = (
                f'Bad request.\nCould not find service "{label}" in domain for user gui: {uid}\n'
            )
            return subprocess.CompletedProcess(argv, 113, "", stderr)
        self.commands.append(argv)
        if self.on_command is not None and (result := self.on_command(argv)) is not None:
            return result
        if argv[1] == "bootout":
            self.jobs.pop(argv[2], None)
        return subprocess.CompletedProcess(argv, 0, "", "")


def operation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake: FakeLaunchd
) -> dict[str, JsonValue]:
    """A journaled, planned but unattempted macOS launch for one release request."""
    monkeypatch.setattr(macos, "run_bounded", fake)
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
        platform_tag="macOS-fixture",
        machine="test",
        previous=old,
        candidate=new,
        executor=new,
        configuration_digest="f" * 64,
    )
    (home / "releases").mkdir()
    (home / "releases/current-release").write_text(
        json.dumps(
            {
                "artifact_digest": request.previous.artifact_digest,
                "manifest_digest": request.previous.manifest_digest,
            }
        )
    )
    current = journal.create(request)
    root = home / "releases" / new.artifact_digest
    (root / "venv/bin").mkdir(parents=True)
    (root / "site").mkdir()
    runtime = VerifiedRelease(
        new.artifact_digest, new.manifest_digest, root, root / "venv/bin/python", root / "site"
    )

    def verified(*_args: object, **_kwargs: object) -> VerifiedRelease:
        return runtime

    monkeypatch.setattr(ReleaseRef, "verify", verified)
    monkeypatch.setattr(macos, "_boot_id", lambda: "boot-a")
    monkeypatch.setattr(finite_artifact, "capture", lambda _home: HELPER)
    plan = macos.plan_launch(request.path, runtime)
    journal.Journal(current).record_launch(plan)
    return plan


class Harness:
    """One planned macOS operation with controllable native facts."""

    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.fake = FakeLaunchd()
        self.plan = operation(tmp_path, monkeypatch, self.fake)
        self.launch = macos.DarwinLaunch.model_validate(self.plan)
        self.path = Path(self.launch.operation)
        self.alive: set[OwnedProcess] = {HELPER_BIRTH, EXECUTOR_BIRTH}
        self.group_alive = False
        self.helper = HELPER_BIRTH
        self.executor: OwnedProcess | None = EXECUTOR_BIRTH
        harness = self

        def live(process: OwnedProcess) -> bool:
            return process in harness.alive

        def helper(_launch: macos.DarwinLaunch, pid: int) -> OwnedProcess:
            assert pid == harness.helper.pid
            return harness.helper

        monkeypatch.setattr(OwnedProcess, "live", live)
        monkeypatch.setattr(macos, "_helper", helper)
        monkeypatch.setattr(macos, "_executor", lambda _launch, _helper: harness.executor)
        monkeypatch.setattr(macos, "_require_group_tree", lambda _helper: None)
        # The fixture artifact path does not exist; the pre-bootstrap program
        # check has its own test against real files.
        monkeypatch.setattr(macos, "_require_launchable", lambda _planned: None)
        monkeypatch.setattr(
            launchd_custody, "group_empty", lambda _pgid, _helper: not harness.group_alive
        )
        # Escalation and evidence never touch the host's real process groups here.
        self.pinned = False
        monkeypatch.setattr(launchd_custody, "_pinned", lambda _executor, _pgid: harness.pinned)
        monkeypatch.setattr(launchd_custody, "group_members", lambda _pgid: (set(), set()))
        monkeypatch.setattr(macos, "_SETTLE_S", 0.3)
        monkeypatch.setattr(launchd_custody, "_CLOSURE_WAIT_S", 0.1)
        monkeypatch.setattr(launchd_custody, "_KILL_WAIT_S", 0.1)

    def running(self, launch: macos.DarwinLaunch | None = None, **kwargs: Any) -> None:
        """The helper runs and has published its group receipt before spawning."""
        launch = launch or self.launch
        kwargs.setdefault("pid", self.helper.pid)
        write_group_receipt(launch, kwargs["pid"], kwargs.get("asid", ASID))
        self.fake.jobs[launch.target] = render(launch, **kwargs)

    def terminal(self, launch: macos.DarwinLaunch | None = None, **kwargs: Any) -> None:
        launch = launch or self.launch
        self.alive.clear()
        self.fake.jobs[launch.target] = render(launch, state="not running", **kwargs)

    def launched(self) -> macos.DarwinJob:
        """Bootstrap through the adapter; the fake job starts running on bootstrap."""

        def start(argv: list[str]) -> None:
            if argv[1] == "bootstrap":
                loaded = next(
                    macos.DarwinLaunch.model_validate(record)
                    for record in [journal.read_operation(self.path).launch]
                    if record is not None
                )
                assert argv[3] == loaded.plist
                self.running(loaded)

        self.fake.on_command = start
        return macos.launch(self.plan)

    def record_native(self, job: macos.DarwinJob) -> None:
        with journal.exclusive(self.path) as current:
            current.record_native(job.identity)


@pytest.fixture
def harness(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Harness:
    return Harness(tmp_path, monkeypatch)
