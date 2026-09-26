"""macOS executor retirement and continuation through the one operation journal."""

from __future__ import annotations

import subprocess

import pytest

from cli.release_transition import journal
from cli.release_transition import launcher_macos as macos
from services.permissions_helper import finite_artifact
from shared.native_process.ownership import OwnedProcess
from tests.lifecycle.transition.macos.launchd_fake import (
    EXECUTOR_BIRTH,
    HELPER,
    HELPER_BIRTH,
    Harness,
)
from tests.lifecycle.transition.macos.launchd_fake import harness as harness


def _advance(harness: Harness, *phases: journal.Phase) -> None:
    with journal.exclusive(harness.path) as current:
        for phase in phases:
            current.advance(phase)


def test_retirement_records_intent_before_exact_bootout_and_requires_absence(
    harness: Harness,
) -> None:
    harness.record_native(harness.launched())
    harness.terminal(exit_code=0)
    observed: list[str | None] = []

    def bootout(argv: list[str]) -> None:
        assert argv == [macos.LAUNCHCTL, "bootout", harness.launch.target]
        retirement = journal.read_operation(harness.path).retirement
        observed.append(None if retirement is None else retirement.state)
        harness.fake.jobs.pop(argv[2])

    harness.fake.on_command = bootout
    terminal = macos.retire_current(harness.plan)
    assert observed == ["requested"]
    current = journal.read_operation(harness.path)
    assert current.retirement is not None and current.retirement.state == "absent"
    assert current.retirement.terminal == terminal.model_dump(mode="json")
    assert terminal.closed == {
        "helper": macos.Birth.of(HELPER_BIRTH),
        "executor": macos.Birth.of(EXECUTOR_BIRTH),
    }
    # Replaying the completed receipt needs no native answer beyond absence.
    assert macos.retire_current(harness.plan) == terminal
    assert [argv[1] for argv in harness.fake.commands] == ["bootstrap", "bootout"]
    harness.terminal(exit_code=0)
    with pytest.raises(RuntimeError, match="reappeared"):
        macos.retire_current(harness.plan)


def test_bootout_without_positive_absence_keeps_requested_retirement(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness.record_native(harness.launched())
    harness.terminal(signal=9)
    monkeypatch.setattr(macos, "_QUERY_TIMEOUT_S", 0.2)

    def ignored(argv: list[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(argv, 0, "", "")

    harness.fake.on_command = ignored
    with pytest.raises(RuntimeError, match="retirement unresolved"):
        macos.retire_current(harness.plan)
    retirement = journal.read_operation(harness.path).retirement
    assert retirement is not None and retirement.state == "requested"


def test_living_executor_is_never_retired(harness: Harness) -> None:
    harness.record_native(harness.launched())
    with pytest.raises(RuntimeError, match="not positively closed"):
        macos.retire_current(harness.plan)
    assert journal.read_operation(harness.path).retirement is None
    assert [argv[1] for argv in harness.fake.commands] == ["bootstrap"]


def test_closure_evidence_cannot_change_between_intent_and_bootout(harness: Harness) -> None:
    harness.record_native(harness.launched())
    harness.terminal(exit_code=80)

    def interrupted(argv: list[str]) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(argv, 30)

    harness.fake.on_command = interrupted
    with pytest.raises(RuntimeError, match="outcome unknown"):
        macos.retire_current(harness.plan)
    harness.terminal(exit_code=0)
    harness.fake.on_command = None
    with pytest.raises(RuntimeError, match="changed after closure"):
        macos.retire_current(harness.plan)


def test_unrecorded_executor_closes_without_an_invented_birth(harness: Harness) -> None:
    harness.launched()
    harness.terminal(exit_code=71)
    terminal = macos.retire_current(harness.plan)
    assert terminal.closed is None and terminal.exit_code == 71
    assert macos.FINITE_EXIT[71] == "spawn-failed"


def test_resume_continues_the_same_decision_under_a_new_attempt(harness: Harness) -> None:
    job = harness.launched()
    harness.record_native(job)
    _advance(harness, "quiescing", "stopping")
    harness.terminal(signal=9)
    helper, executor = OwnedProcess(910, 5.5, None), OwnedProcess(911, 6.5, None)
    harness.helper, harness.executor = helper, executor
    harness.alive.update({helper, executor})
    resumed = macos.resume(harness.plan)
    current = journal.read_operation(harness.path)
    assert (current.attempt, current.phase, current.direction) == (1, "stopping", "candidate")
    retired = current.retired_executors[0]
    assert retired["attempt"] == 0 and retired["launch"] == harness.plan
    assert retired["native"] == job.identity
    assert retired["terminal"] == current.retired_executors[0]["terminal"]
    assert current.launch is not None and current.launch_attempted
    replacement = macos.DarwinLaunch.model_validate(current.launch)
    assert replacement.label.endswith(".a1") and replacement.label != harness.launch.label
    assert replacement.helper == HELPER
    assert [argv[1] for argv in harness.fake.commands] == ["bootstrap", "bootout", "bootstrap"]
    assert resumed.helper == macos.Birth.of(helper) and resumed.pgid == helper.pid
    assert resumed.executor == macos.Birth.of(executor)
    with pytest.raises(ValueError, match="durable intent"):
        macos.resume(harness.plan)


def test_resume_refuses_a_changed_signed_helper_before_relaunch(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness.record_native(harness.launched())
    harness.terminal(exit_code=81)
    upgraded = HELPER.model_copy(update={"sha256": "b" * 64})
    monkeypatch.setattr(finite_artifact, "capture", lambda: upgraded)
    with pytest.raises(RuntimeError, match="signed helper changed"):
        macos.resume(harness.plan)
    current = journal.read_operation(harness.path)
    assert current.attempt == 0 and current.retired_executors == ()
    assert current.retirement is not None and current.retirement.state == "absent"
    assert [argv[1] for argv in harness.fake.commands] == ["bootstrap", "bootout"]


def test_completed_operation_never_resumes_and_settled_history_survives_reboot(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness.record_native(harness.launched())
    _advance(
        harness,
        "quiescing",
        "stopping",
        "selecting",
        "starting",
        "observing",
        "resuming",
        "complete",
    )
    harness.terminal(exit_code=0)
    with pytest.raises(RuntimeError, match="cannot launch another executor"):
        macos.resume(harness.plan)
    terminal = macos.retire_current(harness.plan)
    before = len(harness.fake.commands)
    # A later boot on another OS build: settled absence replays without a plan.
    monkeypatch.setattr(macos, "_boot_id", lambda: "boot-b")
    monkeypatch.setattr(macos, "_macos", lambda: ("27.0", "27A100"))
    assert macos.retire_current(harness.plan) == terminal
    assert len(harness.fake.commands) == before
    with pytest.raises(ValueError, match="durable intent in this boot"):
        macos.readback(harness.plan)
    harness.terminal(exit_code=0)
    with pytest.raises(RuntimeError, match="reappeared"):
        macos.retire_current(harness.plan)
