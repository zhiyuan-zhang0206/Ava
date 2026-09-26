"""macOS release start through the persistent home helper: journaled custody and refusals.

The helper, keeper, root and data plane are fakes with controllable native
facts; the journal, the custody rules and the pinned-seed comparison are real.
Native proof lives in test_root_macos_native.py.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from cli.release_transition import journal, root_macos, root_service
from cli.release_transition.launchd_custody import Birth, RootCustody
from cli.release_transition.request import Request
from services.permissions_helper import client, finite_artifact
from shared import paths
from shared.native_process import ownership
from shared.native_process.ownership import OwnedProcess
from shared.root_control import client as root_client
from shared.runtime_release import VerifiedRelease
from tests.lifecycle.transition.macos.launchd_fake import HELPER, Harness
from tests.lifecycle.transition.macos.launchd_fake import harness as harness

HOME_HELPER = OwnedProcess(700, 7.5, None)
RESTARTED_HELPER = OwnedProcess(702, 12.5, None)
ROOT = OwnedProcess(710, 8.5, None)
RESTARTED_ROOT = OwnedProcess(711, 9.5, None)
EXECUTABLE = Path(HELPER.executable)
_REAL_START_ACTION = root_macos._run_start_action


class World:
    """One home helper, its keeper and root, and the local data plane, all as native facts."""

    def __init__(self, harness: Harness, monkeypatch: pytest.MonkeyPatch) -> None:
        self.harness = harness
        request = journal.read_operation(harness.path).request
        assert isinstance(request, Request)
        self.home = Path(request.home)
        self.run_dir = self.home / "run/ava-root"
        self.run_dir.mkdir(parents=True)
        candidate = self.home / "releases" / request.candidate.artifact_digest
        self.image = VerifiedRelease(
            request.candidate.artifact_digest,
            request.candidate.manifest_digest,
            candidate,
            candidate / "venv/bin/python",
            candidate / "site",
        )
        self.helper = HOME_HELPER
        self.artifact = HELPER
        self.ancestors: set[int] = set()
        self.keeper: dict[str, Any] = {
            "state": "stopped",
            "seeded": True,
            "restarts": 0,
            "stop_requested": True,
            "run_dir": str(self.run_dir),
        }
        self.root: OwnedProcess | None = None
        self.parent: dict[int, int] = {}
        self.launch: dict[int, dict[str, Any]] = {}
        self.data = {
            "postgres": OwnedProcess(800, 1.0, None),
            "redis": OwnedProcess(801, 1.0, None),
        }
        self.data_after: dict[str, OwnedProcess] | None = None
        self.events: list[str] = []
        self.on_stage: Any = None
        harness.alive.add(HOME_HELPER)
        self._patch(monkeypatch)

    def _patch(self, monkeypatch: pytest.MonkeyPatch) -> None:
        world = self
        # Tick-less macOS births compare by create time only on darwin; a Linux
        # runner would refuse every such comparison as unknown evidence.
        monkeypatch.setattr(ownership, "sys", SimpleNamespace(platform="darwin"))

        def home_helper(home: Path) -> tuple[OwnedProcess, Path]:
            assert home == world.home
            world.events.append("authenticate")
            return world.helper, EXECUTABLE

        def signed(executable: Path, home: Path) -> Any:
            assert (executable, home) == (EXECUTABLE, world.home)
            return world.artifact

        def root_status(*, sock_path: Path) -> dict[str, Any]:
            assert sock_path == world.home / "run/helper.sock"
            return dict(world.keeper)

        def ping_peer(*, sock_path: Path) -> tuple[dict[str, Any], int]:
            assert sock_path == world.home / "run/helper.sock"
            return {"pong": True}, world.helper.pid

        class Process:
            def __init__(self, pid: int | None = None) -> None:
                self.pid = os.getpid() if pid is None else pid

            def parents(self) -> list[SimpleNamespace]:
                return [SimpleNamespace(pid=pid) for pid in world.ancestors]

            def ppid(self) -> int:
                return world.parent[self.pid]

            def cmdline(self) -> list[str]:
                return world.launch[self.pid]["argv"]

            def cwd(self) -> str:
                return world.launch[self.pid]["cwd"]

        def data_plane() -> dict[str, OwnedProcess]:
            world.events.append("data-plane")
            if world.data_after is not None and "stage" in world.events:
                return world.data_after
            return dict(world.data)

        def stage(operation: journal.Operation, image: VerifiedRelease) -> None:
            assert operation.phase == "starting" and operation.root is not None
            intent = RootCustody.model_validate(operation.root)
            assert intent.root is None, "the start effect must follow a durable intent"
            world.events.append("stage")
            if world.on_stage is not None:
                world.on_stage()
                return
            world.run(ROOT, image)

        def observe(operation: journal.Operation, image: VerifiedRelease) -> None:
            assert image == world.image
            world.events.append("observe-selected")

        monkeypatch.setattr(finite_artifact, "home_helper", home_helper)
        monkeypatch.setattr(finite_artifact, "signed_artifact", signed)
        monkeypatch.setattr(client, "root_status", root_status)
        monkeypatch.setattr(client, "ping_peer", ping_peer)
        monkeypatch.setattr(root_macos.psutil, "Process", Process)
        monkeypatch.setattr(
            paths, "permissions_helper_socket", lambda: world.home / "run/helper.sock"
        )
        monkeypatch.setattr(paths, "root_run_dir", lambda: world.run_dir)
        monkeypatch.setattr(root_client, "root_process", lambda: world.root)
        monkeypatch.setattr(root_macos, "_data_plane", data_plane)
        monkeypatch.setattr(root_macos, "_run_start_action", stage)
        monkeypatch.setattr(root_macos, "observe_selected", observe)

    def seed(self, image: VerifiedRelease) -> dict[str, Any]:
        return {
            "argv": [
                *image.module_argv("services.ava_root"),
                "--run-dir",
                str(self.run_dir),
                "--manifests",
                str(self.run_dir / "manifests.json"),
            ],
            "cwd": str(image.cwd),
            "run_dir": str(self.run_dir),
            "stdout": str(self.run_dir / "root.stdout.log"),
            "stderr": str(self.run_dir / "root.stderr.log"),
        }

    def run(self, root: OwnedProcess, image: VerifiedRelease) -> None:
        """What ordinary start does on macOS: persist the seed, the keeper spawns root."""
        seed = self.seed(image)
        (self.run_dir / "seed.json").write_text(json.dumps(seed | {"env": {"SECRET": "x"}}))
        self.keeper.update(state="running", pid=root.pid, stop_requested=False, seed=seed)
        self.harness.alive.add(root)
        self.root = root
        self.parent[root.pid] = self.helper.pid
        self.launch[root.pid] = {"argv": seed["argv"], "cwd": seed["cwd"]}

    def crash_restart(self, replacement: OwnedProcess, image: VerifiedRelease) -> None:
        """The keeper respawns an unexpectedly exited root from its held seed."""
        assert self.root is not None
        self.harness.alive.discard(self.root)
        self.keeper["restarts"] += 1
        self.run(replacement, image)

    def custody(self) -> RootCustody | None:
        record = journal.read_operation(self.harness.path).root
        return None if record is None else RootCustody.model_validate(record)


def _starting(harness: Harness) -> None:
    harness.launched()
    with journal.exclusive(harness.path) as current:
        for phase in ("quiescing", "stopping", "selecting", "starting"):
            current.advance(phase)


@pytest.fixture
def world(harness: Harness, monkeypatch: pytest.MonkeyPatch) -> World:
    _starting(harness)
    return World(harness, monkeypatch)


def _start(world: World) -> None:
    with journal.exclusive(world.harness.path) as current:
        root_macos.start(current, world.image)


def test_start_journals_intent_before_the_effect_and_the_root_receipt_after(world: World) -> None:
    _start(world)
    assert world.events == [
        "authenticate",
        "data-plane",
        "stage",
        "data-plane",
        "authenticate",
        # observation: fresh selected readiness, then the journaled custody
        "observe-selected",
        "authenticate",
    ]
    assert world.custody() == RootCustody(
        direction="candidate", helper=Birth.of(HOME_HELPER), restarts=0, root=Birth.of(ROOT)
    )


@pytest.mark.parametrize(
    "keeper",
    [
        {"state": "running", "pid": 999, "stop_requested": False},
        {"state": "backoff", "stop_requested": False},
        {"state": "stopped", "stop_requested": False},
        {"state": "conflict", "stop_requested": True},
    ],
)
def test_first_start_refuses_a_keeper_that_holds_or_may_restart_a_root(
    world: World, keeper: dict[str, Any]
) -> None:
    world.keeper.update(keeper)
    with pytest.raises(RuntimeError, match="keeps or may restart a root"):
        _start(world)
    assert "stage" not in world.events and world.custody() is None


def test_never_seeded_keeper_is_a_valid_first_start(world: World) -> None:
    world.keeper.update(state="unseeded", seeded=False, stop_requested=False)
    _start(world)
    custody = world.custody()
    assert custody is not None and custody.root == Birth.of(ROOT)


def test_retry_after_the_effect_keeps_the_baseline_and_accepts_the_same_root(world: World) -> None:
    def killed_after_seeding() -> None:
        world.run(ROOT, world.image)
        raise RuntimeError("stage process killed after root_seed")

    world.on_stage = killed_after_seeding
    with pytest.raises(RuntimeError, match="killed after root_seed"):
        _start(world)
    intent = world.custody()
    assert intent is not None and intent.root is None
    # The retry reuses the live identical root (ordinary start is idempotent).
    world.on_stage = lambda: None
    _start(world)
    assert world.custody() == intent.model_copy(update={"root": Birth.of(ROOT)})


def test_keeper_restart_before_the_receipt_is_never_accepted(world: World) -> None:
    def killed_after_seeding() -> None:
        world.run(ROOT, world.image)
        raise RuntimeError("stage process killed after root_seed")

    world.on_stage = killed_after_seeding
    with pytest.raises(RuntimeError):
        _start(world)
    world.crash_restart(RESTARTED_ROOT, world.image)
    world.on_stage = lambda: None
    with pytest.raises(ValueError, match="intent's direction, helper or baseline"):
        _start(world)
    custody = world.custody()
    assert custody is not None and custody.root is None and custody.restarts == 0


def test_recorded_receipt_is_only_observed_never_started_again(world: World) -> None:
    _start(world)
    world.events.clear()
    _start(world)
    assert "stage" not in world.events and "observe-selected" in world.events


def test_receipted_root_replaced_by_the_keeper_refuses_instead_of_restarting(world: World) -> None:
    _start(world)
    world.crash_restart(RESTARTED_ROOT, world.image)
    world.events.clear()
    with pytest.raises(RuntimeError, match="keeper replaced the started root"):
        _start(world)
    assert "stage" not in world.events


def test_restarted_helper_before_the_receipt_starts_a_new_intent(world: World) -> None:
    def rebooted() -> None:
        raise RuntimeError("reboot before the root")

    world.on_stage = rebooted
    with pytest.raises(RuntimeError, match="reboot"):
        _start(world)
    world.helper = RESTARTED_HELPER
    world.harness.alive.add(RESTARTED_HELPER)
    world.on_stage = None
    _start(world)
    assert world.custody() == RootCustody(
        direction="candidate", helper=Birth.of(RESTARTED_HELPER), restarts=0, root=Birth.of(ROOT)
    )


def test_data_plane_birth_change_refuses_before_any_receipt(world: World) -> None:
    world.data_after = world.data | {"postgres": OwnedProcess(890, 3.0, None)}
    with pytest.raises(RuntimeError, match="data-plane births"):
        _start(world)
    custody = world.custody()
    assert custody is not None and custody.root is None


_KEEPER_FAULTS: dict[str, dict[str, Any]] = {
    "keeper-pid": {"pid": 4321},
    "restarts": {"restarts": 1},
    "stop-intent": {"stop_requested": True},
    "run-dir": {"run_dir": "/elsewhere"},
}


def _break(world: World, fault: str) -> None:
    if fault in _KEEPER_FAULTS:
        world.keeper.update(_KEEPER_FAULTS[fault])
    elif fault.startswith(("persisted-", "reported-", "cwd", "live-", "no-report")):
        _break_seed(world, fault)
    else:
        _break_owner(world, fault)


def _break_owner(world: World, fault: str) -> None:
    match fault:
        case "artifact":
            world.artifact = HELPER.model_copy(update={"sha256": "b" * 64})
        case "ancestor":
            world.ancestors.add(HOME_HELPER.pid)
        case "helper-birth":
            world.helper = RESTARTED_HELPER
            world.harness.alive.add(RESTARTED_HELPER)
            world.parent[ROOT.pid] = RESTARTED_HELPER.pid
        case "root-birth":
            world.root = RESTARTED_ROOT
            world.harness.alive.add(RESTARTED_ROOT)
            world.parent[RESTARTED_ROOT.pid] = HOME_HELPER.pid
            world.keeper["pid"] = RESTARTED_ROOT.pid
            world.launch[RESTARTED_ROOT.pid] = world.launch[ROOT.pid]
        case "root-dead":
            world.root = None
        case "parent":
            world.parent[ROOT.pid] = 1
        case _:
            raise AssertionError(fault)


def _break_seed(world: World, fault: str) -> None:
    seed = world.seed(world.image)
    match fault:
        case "no-report":
            del world.keeper["seed"]
        case "persisted-argv":
            (world.run_dir / "seed.json").write_text(json.dumps(seed | {"argv": ["/moving"]}))
        case "persisted-not-object":
            (world.run_dir / "seed.json").write_text("[]")
        case "reported-selector":
            moving = ["/home/releases/current/venv/bin/python", *seed["argv"][1:]]
            world.keeper["seed"] = seed | {"argv": moving}
            (world.run_dir / "seed.json").write_text(json.dumps(seed | {"argv": moving}))
            world.launch[ROOT.pid]["argv"] = moving
        case "cwd":
            world.keeper["seed"] = seed | {"cwd": "/elsewhere"}
            (world.run_dir / "seed.json").write_text(json.dumps(seed | {"cwd": "/elsewhere"}))
            world.launch[ROOT.pid]["cwd"] = "/elsewhere"
        case "live-argv":
            world.launch[ROOT.pid]["argv"] = [*seed["argv"], "--extra"]
        case "live-cwd":
            world.launch[ROOT.pid]["cwd"] = "/elsewhere"
        case _:
            raise AssertionError(fault)


@pytest.mark.parametrize(
    ("fault", "reason"),
    [
        ("artifact", "differs from the operation's signed helper artifact"),
        ("ancestor", "cannot be an ancestor"),
        ("helper-birth", "helper was replaced"),
        ("root-birth", "keeper replaced the started root"),
        ("root-dead", "not live"),
        ("parent", "keeper's live child"),
        ("keeper-pid", "keeper's live child"),
        ("restarts", "keeper replaced the started root"),
        ("stop-intent", "keeper's live child"),
        ("run-dir", "keeper's live child"),
        ("no-report", "does not report its retained root seed"),
        ("persisted-argv", "not pinned"),
        ("persisted-not-object", "not pinned"),
        ("reported-selector", "not pinned"),
        ("cwd", "not pinned"),
        ("live-argv", "not pinned"),
        ("live-cwd", "not pinned"),
    ],
)
def test_observed_custody_refuses_every_changed_fact(world: World, fault: str, reason: str) -> None:
    _start(world)
    operation = journal.read_operation(world.harness.path)
    root_macos.require_custody(operation, world.image)
    _break(world, fault)
    with pytest.raises(RuntimeError, match=reason):
        root_macos.observe(operation, world.image)
    with pytest.raises(RuntimeError, match=reason):
        root_macos.restore_boot(operation, world.image)


def test_observation_requires_a_receipt_of_the_current_direction(world: World) -> None:
    operation = journal.read_operation(world.harness.path)
    with pytest.raises(RuntimeError, match="no journaled start receipt"):
        root_macos.require_custody(operation, world.image)
    _start(world)
    with journal.exclusive(world.harness.path) as current:
        current.recover("candidate failed")
    with pytest.raises(RuntimeError, match="no journaled start receipt"):
        root_macos.require_custody(journal.read_operation(world.harness.path), world.image)


@pytest.mark.parametrize(
    ("keeper", "stopped"),
    [
        ({"state": "stopped", "stop_requested": True}, True),
        ({"state": "unseeded", "seeded": False, "stop_requested": False}, True),
        ({"state": "running", "pid": 710, "stop_requested": False}, False),
        ({"state": "stopping", "pid": 710, "stop_requested": True}, False),
        ({"state": "backoff", "stop_requested": False}, False),
        ({"state": "stopped", "stop_requested": False}, False),
        ({"state": "conflict", "stop_requested": True}, False),
    ],
)
def test_stop_requires_durable_keeper_stop_intent(
    world: World, keeper: dict[str, Any], *, stopped: bool
) -> None:
    world.keeper.update(keeper)
    operation = journal.read_operation(world.harness.path)
    if stopped:
        root_macos.require_stopped(operation)
    else:
        with pytest.raises(RuntimeError, match="may still restart"):
            root_macos.require_stopped(operation)


def test_keeper_reads_are_bracketed_by_the_same_helper_peer(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    def moved(**_kwargs: object) -> tuple[dict[str, bool], int]:
        return {"pong": True}, RESTARTED_HELPER.pid

    monkeypatch.setattr(client, "ping_peer", moved)
    with pytest.raises(RuntimeError, match="changed while its root keeper was read"):
        _start(world)
    assert world.custody() is None


def test_helper_root_start_admits_only_a_launched_release(world: World) -> None:
    operation = journal.read_operation(world.harness.path)
    for changed in (
        operation.model_copy(update={"launch": None}),
        operation.model_copy(update={"direction": None}),
    ):
        with pytest.raises(RuntimeError, match="only a launched macOS release"):
            root_macos.verified_helper(changed)


@pytest.mark.parametrize("outcome", ["ok", "failed", "timeout"])
def test_start_action_runs_the_selected_image_stage_as_a_bounded_finite_tool(
    world: World, monkeypatch: pytest.MonkeyPatch, outcome: str
) -> None:
    import pwd

    from shared import proc

    calls: list[dict[str, Any]] = []

    def bounded(argv: list[str], **kwargs: Any) -> Any:
        calls.append({"argv": argv, **kwargs})
        if outcome == "timeout":
            raise subprocess.TimeoutExpired(argv, kwargs["timeout"])
        return SimpleNamespace(returncode=int(outcome == "failed"), stderr="stage refused\n")

    monkeypatch.setattr(proc, "run_bounded", bounded)
    operation = journal.read_operation(world.harness.path)
    if outcome == "ok":
        _REAL_START_ACTION(operation, world.image)
    else:
        with pytest.raises(RuntimeError, match=r"stage refused|exceeded its bound"):
            _REAL_START_ACTION(operation, world.image)
    (call,) = calls
    assert call["argv"] == [
        *world.image.module_argv("cli.release_transition.stage"),
        "--operation",
        str(operation.request.path),
    ]
    assert call["cwd"] == world.image.cwd and call["timeout"] == root_macos.START_TIMEOUT_S
    # Start and observation share one fixed environment: the launch digest repeats.
    account = Path(pwd.getpwuid(os.getuid()).pw_dir)
    assert call["env"] == dict(
        root_service.stage_environment(world.home, Path(operation.request.registry), account)
    )


def test_data_plane_comparison_is_exact_births(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ownership, "sys", SimpleNamespace(platform="darwin"))  # tick-less births
    before = {"postgres": OwnedProcess(1, 1.0, None), "redis": OwnedProcess(2, 1.0, None)}
    root_macos._require_same_data_plane(before, dict(before))
    for after in (
        {"postgres": before["postgres"]},
        before | {"pgbouncer": OwnedProcess(3, 1.0, None)},
        before | {"redis": OwnedProcess(2, 1.5, None)},
    ):
        with pytest.raises(RuntimeError, match="data-plane births"):
            root_macos._require_same_data_plane(before, after)


def test_executor_kill_keeps_root_custody_so_the_next_attempt_only_verifies(world: World) -> None:
    from cli.release_transition import launcher_macos as macos

    _start(world)
    before = journal.read_operation(world.harness.path).root
    world.harness.terminal(signal=9)
    helper, executor = OwnedProcess(910, 5.5, None), OwnedProcess(911, 6.5, None)
    world.harness.helper, world.harness.executor = helper, executor
    world.harness.alive.update({helper, executor, HOME_HELPER, ROOT})
    macos.resume(world.harness.plan)
    current = journal.read_operation(world.harness.path)
    assert (current.attempt, current.phase, current.root) == (1, "starting", before)
    world.events.clear()
    _start(world)
    assert "stage" not in world.events and "observe-selected" in world.events
