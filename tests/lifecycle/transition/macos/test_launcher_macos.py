"""macOS finite launch authority and native readback; no native job is mutated."""

from __future__ import annotations

import hashlib
import os
import plistlib
import stat
import subprocess
from collections.abc import Callable
from itertools import count
from pathlib import Path
from typing import Any
from uuid import uuid4

import psutil
import pytest

from cli.release_transition import journal
from cli.release_transition import launcher_macos as macos
from cli.release_transition.launchd_print import LaunchdPendingSpawnError
from cli.release_transition.request import PitrRequest
from shared.native_process.ownership import OwnedProcess
from tests.lifecycle.transition.macos.launchd_fake import (
    EXECUTOR_BIRTH,
    HELPER,
    HELPER_BIRTH,
    Harness,
    render,
)
from tests.lifecycle.transition.macos.launchd_fake import harness as harness


def test_plan_is_pure_and_renders_the_exact_private_job(harness: Harness) -> None:
    launch = harness.launch
    document = macos.plist_document(launch)
    assert plistlib.loads(document) == {
        "Label": launch.label,
        "ProgramArguments": launch.program_arguments(),
        "WorkingDirectory": launch.cwd,
        "RunAtLoad": True,
        "KeepAlive": False,
        "AbandonProcessGroup": False,
        "ExitTimeOut": macos.EXIT_TIMEOUT_S,
        "ProcessType": "Standard",
        "Umask": 0o022,
        "StandardOutPath": launch.stdout,
        "StandardErrorPath": launch.stderr,
    }
    assert hashlib.sha256(document).hexdigest() == launch.plist_sha256
    digest = hashlib.sha256(launch.operation.encode()).hexdigest()[:32]
    assert launch.label == f"com.ava.release-executor.{digest}.a0"
    assert Path(launch.plist).parent == harness.path.parent / "executor" / "a0"
    assert "LaunchAgents" not in launch.plist
    assert launch.helper == HELPER
    assert set(launch.environment) == {"HOME", "AVA_HOME", "AVA_CLUSTER_REGISTRY", "PATH"}
    arguments = launch.program_arguments()
    assert arguments[:5] == [HELPER.executable, "--finite-executor", "v1", "--cwd", launch.cwd]
    assert arguments[arguments.index("--") + 1 :] == launch.argv
    assert launch.argv[-3:] == ["cli.release_transition.execute", "--operation", launch.operation]
    assert not Path(launch.plist).exists()
    assert harness.fake.commands == []


def test_unsupported_macos_or_changed_boot_refuses_readback(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness.launched()
    monkeypatch.setattr(macos, "_macos", lambda: ("27.0", "27A100"))
    with pytest.raises(RuntimeError, match="verified launchd readback contract"):
        macos.readback(harness.plan)
    monkeypatch.setattr(macos, "_macos", lambda: ("26.6.2", "25G83"))
    monkeypatch.setattr(macos, "_boot_id", lambda: "boot-b")
    # The earlier boot's job cannot be custody, but a job loaded under its
    # label in this boot is unknown, never that attempt's evidence.
    with pytest.raises(RuntimeError, match="loaded in a later boot"):
        macos.readback(harness.plan)


def test_launch_persists_the_attempt_before_one_bootstrap(harness: Harness) -> None:
    observed: list[bool] = []

    def bootstrap(argv: list[str]) -> None:
        observed.append(journal.read_operation(harness.path).launch_attempted)
        plist = Path(argv[3])
        assert plist.read_bytes() == macos.plist_document(harness.launch)
        assert stat.S_IMODE(plist.stat().st_mode) == 0o600
        assert stat.S_IMODE(plist.parent.stat().st_mode) == 0o700
        harness.running()

    harness.fake.on_command = bootstrap
    job = macos.launch(harness.plan)
    assert observed == [True]
    assert harness.fake.commands == [
        [macos.LAUNCHCTL, "bootstrap", harness.launch.domain, harness.launch.plist]
    ]
    assert job.helper == macos.Birth.of(HELPER_BIRTH)
    assert job.executor == macos.Birth.of(EXECUTOR_BIRTH)
    assert job.pgid == HELPER_BIRTH.pid and not job.finished
    identity = job.identity
    assert identity["helper"] != identity["executor"]
    with pytest.raises(RuntimeError, match="already attempted"):
        macos.launch(harness.plan)
    assert len(harness.fake.commands) == 1


def _timeout(argv: list[str]) -> subprocess.CompletedProcess[str]:
    raise subprocess.TimeoutExpired(argv, 30)


def _rejected(argv: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(argv, 5, "", "Bootstrap failed: 5: Input/output error")


@pytest.mark.parametrize("response", [_timeout, _rejected])
def test_lost_bootstrap_response_retains_the_attempt_for_readback_only(
    harness: Harness,
    response: Callable[[list[str]], subprocess.CompletedProcess[str]],
) -> None:
    harness.fake.on_command = response
    with pytest.raises(RuntimeError, match=r"unknown|unresolved"):
        macos.launch(harness.plan)
    assert journal.read_operation(harness.path).launch_attempted
    harness.fake.on_command = None
    with pytest.raises(RuntimeError, match="already attempted"):
        macos.launch(harness.plan)
    # A missing job after an attempted dispatch is never "not dispatched".
    with pytest.raises(RuntimeError, match="absent before retirement"):
        macos.readback(harness.plan)
    with pytest.raises(RuntimeError, match="absent before retirement"):
        macos.retire_current(harness.plan)
    assert [argv[1] for argv in harness.fake.commands] == ["bootstrap"]
    # The job did load: readback of this same attempt recovers custody.
    harness.running()
    assert macos.readback(harness.plan).executor == macos.Birth.of(EXECUTOR_BIRTH)


def test_existing_job_refuses_before_plist_or_dispatch(harness: Harness) -> None:
    harness.running()
    with pytest.raises(RuntimeError, match="already exists"):
        macos.launch(harness.plan)
    assert not journal.read_operation(harness.path).launch_attempted
    assert not Path(harness.launch.plist).exists()
    assert harness.fake.commands == []


@pytest.mark.parametrize(
    "error",
    [
        subprocess.CompletedProcess([], 5, "", "Operation not permitted\n"),
        subprocess.CompletedProcess([], 113, "", "Could not find domain for user gui: 501\n"),
        subprocess.CompletedProcess([], 113, "partial", ""),
    ],
)
def test_failed_queries_are_unknown_never_absence(
    harness: Harness, error: subprocess.CompletedProcess[str]
) -> None:
    harness.fake.print_error = error
    with pytest.raises(RuntimeError, match="cannot read native executor job"):
        macos.launch(harness.plan)
    assert not journal.read_operation(harness.path).launch_attempted
    assert harness.fake.commands == []


def test_query_timeout_retains_custody(harness: Harness, monkeypatch: pytest.MonkeyPatch) -> None:
    def hung(argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        if argv[0] == macos.LAUNCHCTL:
            raise subprocess.TimeoutExpired(argv, 30)
        return harness.fake(argv)

    monkeypatch.setattr(macos, "run_bounded", hung)
    with pytest.raises(RuntimeError, match="timed out"):
        macos.launch(harness.plan)
    assert not journal.read_operation(harness.path).launch_attempted


def _drift(harness: Harness) -> list[dict[str, Any]]:
    arguments = harness.launch.program_arguments()
    return [
        {"arguments": [*arguments, "extra"]},
        {"arguments": [a.replace("PATH=", "PATH=/tmp:") for a in arguments]},
        {"properties": ("runatload", "inferred program", "abandon process group")},
        {"properties": ("runatload", "inferred program", "keepalive")},
        {"runs": 2},
    ]


@pytest.mark.parametrize("index", range(5))
def test_loaded_definition_drift_refuses(harness: Harness, index: int) -> None:
    harness.launched()
    harness.running(**_drift(harness)[index])
    with pytest.raises(RuntimeError, match="differs from its journaled launch"):
        macos.readback(harness.plan)


def test_retained_definition_bytes_are_rechecked(harness: Harness) -> None:
    harness.launched()
    Path(harness.launch.plist).write_bytes(b"replaced")
    with pytest.raises(RuntimeError, match="definition changed"):
        macos.readback(harness.plan)


def test_helper_or_executor_birth_replacement_refuses(harness: Harness) -> None:
    harness.record_native(harness.launched())
    reused = OwnedProcess(HELPER_BIRTH.pid, 9.5, None)
    harness.helper = reused
    harness.alive.add(reused)
    with pytest.raises(RuntimeError, match="finite helper birth changed"):
        macos.readback(harness.plan)
    harness.helper = HELPER_BIRTH
    harness.executor = OwnedProcess(EXECUTOR_BIRTH.pid, 9.5, None)
    with pytest.raises(RuntimeError, match="native executor birth changed"):
        macos.readback(harness.plan)
    harness.executor = EXECUTOR_BIRTH
    harness.running(asid=100099)
    with pytest.raises(RuntimeError, match="job changed from captured custody"):
        macos.readback(harness.plan)


def test_observation_is_bracketed_by_two_matching_queries(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness.launched()
    queries = count()
    running = render(harness.launch)
    moved = render(harness.launch, pid=HELPER_BIRTH.pid + 5)

    def flapping(argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        if argv[:2] == [macos.LAUNCHCTL, "print"]:
            text = running if next(queries) % 2 == 0 else moved
            return subprocess.CompletedProcess(argv, 0, text, "")
        return harness.fake(argv)

    monkeypatch.setattr(macos, "run_bounded", flapping)
    with pytest.raises(RuntimeError, match="changed during native observation"):
        macos.readback(harness.plan)


def test_settle_observes_through_launchd_startup(harness: Harness) -> None:
    starting = render(harness.launch).replace("\tstate = running\n", "\tstate = spawn scheduled\n")

    def bootstrap(_argv: list[str]) -> None:
        harness.fake.jobs[harness.launch.target] = starting

    harness.fake.on_command = bootstrap
    with pytest.raises(RuntimeError, match="not yet observable") as refused:
        macos.launch(harness.plan)
    assert isinstance(refused.value.__cause__, LaunchdPendingSpawnError)
    # The retained attempt is observed later, without another bootstrap.
    harness.executor = None
    harness.running()
    assert macos.readback(harness.plan).executor is None
    harness.executor = EXECUTOR_BIRTH
    assert macos.readback(harness.plan).executor == macos.Birth.of(EXECUTOR_BIRTH)
    assert [argv[1] for argv in harness.fake.commands] == ["bootstrap"]


def test_escape_from_the_job_group_refuses_while_traceable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    escaped = OwnedProcess(950, 3.5, None)
    alive = {HELPER_BIRTH, escaped}
    monkeypatch.setattr(OwnedProcess, "live", lambda process: process in alive)
    monkeypatch.setattr(macos, "capture_tree", lambda _helper: {HELPER_BIRTH, escaped})
    monkeypatch.setattr(
        macos, "_process_group", lambda pid: pid if pid == escaped.pid else HELPER_BIRTH.pid
    )
    with pytest.raises(RuntimeError, match="left the launchd job process group"):
        macos._require_group_tree(HELPER_BIRTH)
    alive.discard(escaped)
    macos._require_group_tree(HELPER_BIRTH)


def test_terminal_requires_recorded_births_closed_and_an_empty_group(harness: Harness) -> None:
    harness.record_native(harness.launched())
    harness.terminal(exit_code=0)
    harness.alive.add(EXECUTOR_BIRTH)
    with pytest.raises(RuntimeError, match="live group members"):
        macos.readback(harness.plan)
    harness.alive.clear()
    harness.group_alive = True
    with pytest.raises(RuntimeError, match="live group members"):
        macos.readback(harness.plan)
    harness.group_alive = False
    job = macos.readback(harness.plan)
    assert job.finished and job.exit_code == 0 and job.signal is None
    assert job.closed == {
        "helper": macos.Birth.of(HELPER_BIRTH),
        "executor": macos.Birth.of(EXECUTOR_BIRTH),
    }
    assert macos.FINITE_EXIT[0] == "executor-succeeded"


def test_executor_receipt_is_only_for_the_recorded_direct_child(harness: Harness) -> None:
    harness.launched()
    with pytest.raises(RuntimeError, match="not the recorded external executor"):
        macos.executor_receipt(harness.plan)
    me = OwnedProcess(os.getpid(), 4.5, None)
    harness.executor = me
    harness.alive.add(me)
    receipt = macos.executor_receipt(harness.plan)
    assert receipt["executor"] == macos.Birth.of(me).model_dump(mode="json")
    assert receipt["helper"] == macos.Birth.of(HELPER_BIRTH).model_dump(mode="json")
    assert receipt["pgid"] == HELPER_BIRTH.pid


def test_launch_inputs_must_print_exactly() -> None:
    for bad in (["a\nb"], [" lead"], ["trail "], [""], ["tab\tin"]):
        with pytest.raises(ValueError, match="printable"):
            macos._require_launch_text(bad)
    macos._require_launch_text(["/a b/c", "--env", "PATH=/x:/y"])


def test_macos_admission_is_a_typed_scope_before_effects(harness: Harness) -> None:
    """PITR refuses; a same-schema release reaches the common release preflight."""
    request = journal.read_operation(harness.path).request
    with pytest.raises(ValueError, match="PITR is not admitted on macOS"):
        macos.admit_request(PitrRequest.model_construct(id=uuid4()))
    macos.admit_request(request)


@pytest.mark.parametrize(
    "error",
    [psutil.NoSuchProcess(900), ProcessLookupError(3, "No such process"), psutil.AccessDenied(900)],
)
def test_process_exit_during_observation_retains_custody(
    harness: Harness, monkeypatch: pytest.MonkeyPatch, error: Exception
) -> None:
    harness.launched()

    def vanished(_launch: macos.DarwinLaunch, _pid: int) -> OwnedProcess:
        raise error

    monkeypatch.setattr(macos, "_helper", vanished)
    with pytest.raises(RuntimeError, match="retain custody"):
        macos.readback(harness.plan)
