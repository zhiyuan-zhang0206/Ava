"""macOS closure without receipts, attempt binding, and recovery after boot or login loss.

Regression guards for the independent review of the finite executor adapter:
closure always proves the published job group empty, a stale attempt's receipt
never lands in a later attempt, launchd's real signal texts close normally, a
reboot or a new login session is positive closure only with the recorded owners
gone, and settled history does not depend on launchd's absence wording.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import JsonValue

from cli.release_transition import execute, journal, native, pitr_submit
from cli.release_transition import launcher_linux as linux
from cli.release_transition import launcher_macos as macos
from cli.release_transition.launchd_print import LaunchdPendingSpawnError
from cli.release_transition.request import PitrRequest
from services.permissions_helper.finite_artifact import HelperArtifact
from shared.native_process.ownership import OwnedProcess
from tests.lifecycle.transition.macos.launchd_fake import (
    ASID,
    EXECUTOR_BIRTH,
    HELPER_BIRTH,
    Harness,
    render,
    write_group_receipt,
)
from tests.lifecycle.transition.macos.launchd_fake import harness as harness

_REAL_BOOT_ID = macos._boot_id
_REAL_LAUNCHABLE = macos._require_launchable


def _commands(harness: Harness) -> list[str]:
    return [argv[1] for argv in harness.fake.commands]


def _next_attempt(harness: Harness) -> None:
    helper, executor = OwnedProcess(910, 5.5, None), OwnedProcess(911, 6.5, None)
    harness.helper, harness.executor = helper, executor
    harness.alive.update({helper, executor})


# P1-2: closure without an executor receipt.


def test_terminal_without_receipt_requires_the_published_group_empty(harness: Harness) -> None:
    harness.launched()  # the helper published its group; the executor recorded nothing
    harness.terminal(signal=9)
    harness.group_alive = True  # a SIGTERM-ignoring member survived launchd's cleanup
    for action in (macos.readback, macos.retire_current, macos.resume):
        with pytest.raises(RuntimeError, match="still has live group members"):
            action(harness.plan)
    current = journal.read_operation(harness.path)
    assert current.retirement is None and current.attempt == 0
    assert _commands(harness) == ["bootstrap"]
    harness.group_alive = False
    job = macos.readback(harness.plan)
    assert job.finished and job.closed is None and job.pgid == HELPER_BIRTH.pid


def test_unpublished_group_proves_nothing_was_spawned(harness: Harness) -> None:
    harness.launched()
    Path(harness.launch.group_receipt).unlink()  # the helper refused before publishing
    harness.terminal(exit_code=73)
    harness.group_alive = True  # never consulted: no spawn preceded the receipt
    job = macos.retire_current(harness.plan)
    assert (job.closed, job.pgid, job.exit_code) == (None, None, 73)
    assert macos.FINITE_EXIT[73] == "group-receipt-failed"


def test_executor_receipt_requires_the_helpers_matching_group_receipt(harness: Harness) -> None:
    harness.record_native(harness.launched())
    receipt = Path(harness.launch.group_receipt)
    receipt.unlink()
    with pytest.raises(RuntimeError, match="executor runs without the helper's group receipt"):
        macos.readback(harness.plan)
    harness.terminal(exit_code=0)
    with pytest.raises(RuntimeError, match="without the helper's pre-spawn group receipt"):
        macos.readback(harness.plan)
    write_group_receipt(harness.launch, HELPER_BIRTH.pid, ASID + 1)
    with pytest.raises(RuntimeError, match="differs from the executor receipt"):
        macos.readback(harness.plan)
    harness.running()
    write_group_receipt(harness.launch, HELPER_BIRTH.pid, ASID + 1)
    with pytest.raises(RuntimeError, match="differs from the running job"):
        macos.readback(harness.plan)


# P2-1: a receipt belongs to exactly one attempt.


def test_stale_attempt_receipt_never_lands_in_the_next_attempt(harness: Harness) -> None:
    stale = harness.launched().identity  # computed by attempt 0's executor
    harness.terminal(signal=9)
    _next_attempt(harness)
    macos.resume(harness.plan)
    current = journal.read_operation(harness.path)
    assert current.attempt == 1 and current.launch is not None
    with journal.exclusive(harness.path) as locked:
        with pytest.raises(ValueError, match="different executor attempt"):
            locked.record_native(stale)
        assert locked.operation.native is None
        # The current attempt's own executor still records its receipt.
        locked.record_native(macos.readback(current.launch).identity)


def test_executor_receipt_is_computed_under_the_lock_for_its_own_attempt(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness.launched()
    harness.terminal(signal=9)
    _next_attempt(harness)
    # execute() exports the captured home to its own process environment; keep
    # that write away from this test process.
    monkeypatch.setattr(execute, "os", SimpleNamespace(environ={}, getpid=os.getpid))
    monkeypatch.setattr("shared.runtime_interpreter.verify_loaded_image", lambda *_a, **_k: None)
    real = journal.exclusive

    @contextmanager
    def relaunched_first(path: Path) -> Generator[journal.Journal]:
        # The attempt-0 executor read its launch, then a controller relaunched.
        macos.resume(harness.plan)
        with real(path) as locked:
            yield locked

    def unexpected(_launch: dict[str, JsonValue]) -> dict[str, JsonValue]:
        pytest.fail("a stale executor must not compute a receipt for the new attempt")

    monkeypatch.setattr(execute, "exclusive", relaunched_first)
    monkeypatch.setattr(execute, "_executor_receipt", unexpected)
    with pytest.raises(RuntimeError, match="retired native launch attempt"):
        execute.execute(harness.path)
    assert journal.read_operation(harness.path).native is None


# P2-2: launchd's real terminating-signal texts.


@pytest.mark.parametrize("number", [5, 6, 11, 30, 31])
def test_crash_and_user_signals_close_like_any_signal(harness: Harness, number: int) -> None:
    harness.record_native(harness.launched())
    harness.terminal(signal=number)
    job = macos.retire_current(harness.plan)
    assert (job.signal, job.exit_code, job.finished) == (number, None, True)


# P2-4: a reboot or a new login session ends the recorded job.


def _advance(harness: Harness) -> None:
    with journal.exclusive(harness.path) as current:
        current.advance("quiescing")
        current.advance("stopping")


def test_reboot_retires_the_earlier_boot_attempt_and_resumes(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    job = harness.launched()
    harness.record_native(job)
    _advance(harness)
    harness.alive.clear()
    del harness.fake.jobs[harness.launch.target]  # private plists are not reloaded at boot
    monkeypatch.setattr(macos, "_boot_id", lambda: "boot-b")
    ended = macos.readback(harness.plan)
    assert (ended.evidence, ended.finished, ended.runs, ended.exit_code) == (
        "boot-changed",
        True,
        None,
        None,
    )
    assert ended.closed == {"helper": job.helper, "executor": job.executor}
    assert (ended.pgid, ended.asid) == (HELPER_BIRTH.pid, ASID)
    _next_attempt(harness)
    resumed = macos.resume(harness.plan)
    current = journal.read_operation(harness.path)
    assert (current.attempt, current.phase, current.direction) == (1, "stopping", "candidate")
    assert current.retired_executors[0]["terminal"] == ended.model_dump(mode="json")
    assert current.launch is not None and current.launch["boot_id"] == "boot-b"
    assert resumed.executor is not None and _commands(harness) == ["bootstrap", "bootstrap"]


def test_reboot_with_the_label_loaded_again_is_unknown(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness.record_native(harness.launched())
    harness.terminal(exit_code=0)
    monkeypatch.setattr(macos, "_boot_id", lambda: "boot-b")
    for action in (macos.readback, macos.retire_current, macos.resume):
        with pytest.raises(RuntimeError, match="loaded in a later boot"):
            action(harness.plan)
    assert journal.read_operation(harness.path).retirement is None


def test_logout_closes_only_after_the_login_session_changed(harness: Harness) -> None:
    job = harness.launched()
    harness.record_native(job)
    _advance(harness)
    harness.alive.clear()
    del harness.fake.jobs[harness.launch.target]  # logout tore down gui/<uid>
    # The same session: a lost bootstrap or an external bootout, not closure.
    with pytest.raises(RuntimeError, match="absent before retirement"):
        macos.resume(harness.plan)
    harness.fake.domain_asid = ASID + 7
    harness.group_alive = True
    with pytest.raises(RuntimeError, match="still has live group members"):
        macos.resume(harness.plan)
    harness.group_alive = False
    ended = macos.readback(harness.plan)
    assert (ended.evidence, ended.asid, ended.closed) == (
        "domain-lost",
        ASID,
        {"helper": job.helper, "executor": job.executor},
    )
    _next_attempt(harness)
    macos.resume(harness.plan)
    current = journal.read_operation(harness.path)
    assert current.attempt == 1 and current.retired_executors[0]["terminal"] == ended.model_dump(
        mode="json"
    )
    assert _commands(harness) == ["bootstrap", "bootstrap"]


def test_logout_before_the_executor_receipt_uses_the_helpers_session(harness: Harness) -> None:
    harness.launched()
    harness.alive.clear()
    del harness.fake.jobs[harness.launch.target]
    harness.fake.domain_asid = ASID + 1
    ended = macos.retire_current(harness.plan)
    assert (ended.evidence, ended.closed, ended.pgid) == ("domain-lost", None, HELPER_BIRTH.pid)
    # With no published group there is no recorded session to compare.
    Path(harness.launch.group_receipt).unlink()
    with pytest.raises(RuntimeError, match="absent before retirement"):
        macos.readback(harness.plan)


@pytest.mark.parametrize(
    "change",
    [
        {"runs": 1},
        {"exit_code": 0},
        {"signal": 9},
        {"evidence": "vanished"},
    ],
)
def test_journal_accepts_ended_domains_only_without_launchd_facts(
    harness: Harness, change: dict[str, JsonValue]
) -> None:
    harness.launched()
    harness.alive.clear()
    del harness.fake.jobs[harness.launch.target]
    harness.fake.domain_asid = ASID + 1
    terminal = macos.readback(harness.plan).model_dump(mode="json")
    with journal.exclusive(harness.path) as current:
        with pytest.raises(ValueError, match="living or unknown executor"):
            current.request_retirement(terminal | change)
        with pytest.raises(ValueError, match="living or unknown executor"):
            current.request_retirement(terminal | {"asid": None})
        current.request_retirement(terminal)


# P3-3: a missing program is refused before bootstrap; a pending spawn is live.


def test_missing_program_refuses_before_bootstrap(
    harness: Harness, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(macos, "_require_launchable", _REAL_LAUNCHABLE)
    with pytest.raises(RuntimeError, match="artifact is missing; refusing before bootstrap"):
        macos.launch(harness.plan)
    assert not journal.read_operation(harness.path).launch_attempted
    assert not Path(harness.launch.plist).exists() and harness.fake.commands == []
    program = tmp_path / "AvaPermissionsHelper"
    program.write_bytes(b"helper")
    artifact = HelperArtifact(
        app=str(tmp_path),
        executable=str(program),
        sha256=macos._sha256(program),
        requirement="identifier x",
    )
    present = harness.launch.model_copy(update={"helper": artifact})
    _REAL_LAUNCHABLE(present)
    with pytest.raises(RuntimeError, match="changed before bootstrap"):
        _REAL_LAUNCHABLE(present.model_copy(update={"cwd": str(tmp_path / "gone")}))
    program.write_bytes(b"other")
    with pytest.raises(RuntimeError, match="changed before bootstrap"):
        _REAL_LAUNCHABLE(present)


def _pending(harness: Harness) -> str:
    text = render(harness.launch, state="not running", exit_code=78)
    triggers = (
        "\tevent triggers = {\n"
        f"\t\t{harness.launch.label} => {{\n"
        "\t\t\tdescriptor = {\n"
        f'\t\t\t\t"Executable" => "{harness.launch.helper.executable}"\n'
        "\t\t\t}\n"
        "\t\t}\n"
        "\t}\n\n"
    )
    text = text.replace("\tstate = not running\n", "\tstate = spawn scheduled\n")
    text = text.replace(
        "\tlast exit code = 78\n", "\tlast exit code = 78: EX_CONFIG\n\n" + triggers
    )
    return text.replace(" | ".join(macos.POLICY), "runatload | penalty box | inferred program")


def test_pending_spawn_is_live_custody(harness: Harness) -> None:
    harness.launched()
    harness.alive.clear()
    harness.fake.jobs[harness.launch.target] = _pending(harness)
    for action in (macos.readback, macos.retire_current, macos.resume):
        with pytest.raises(LaunchdPendingSpawnError, match="pending spawn"):
            action(harness.plan)
    assert journal.read_operation(harness.path).retirement is None
    assert _commands(harness) == ["bootstrap"]


# P3-4: settled replay needs launchd's structural answer, not its wording.


@pytest.mark.parametrize(
    ("returncode", "stderr"),
    [(113, "Bad request.\nNo such service (reworded)\n"), (112, "Could not find domain\n")],
)
def test_settled_replay_accepts_structural_absence_after_rewording(
    harness: Harness, returncode: int, stderr: str
) -> None:
    harness.record_native(harness.launched())
    harness.terminal(exit_code=0)
    terminal = macos.retire_current(harness.plan)
    harness.fake.print_error = subprocess.CompletedProcess([], returncode, "", stderr)
    assert macos.retire_current(harness.plan) == terminal
    harness.fake.print_error = subprocess.CompletedProcess([], 5, "", "Input/output error\n")
    with pytest.raises(RuntimeError, match="cannot read native executor job"):
        macos.retire_current(harness.plan)


# P3-5 and P3-6: typed refusals and the one dispatch point.


@pytest.mark.parametrize(
    "error",
    [
        subprocess.CalledProcessError(1, ["sysctl"]),
        subprocess.TimeoutExpired(["sysctl"], 5),
        ValueError("badly formed hexadecimal UUID string"),
    ],
)
def test_boot_identity_read_failures_are_typed_refusals(
    monkeypatch: pytest.MonkeyPatch, error: Exception
) -> None:
    def failing() -> str:
        raise error

    monkeypatch.setattr(macos, "native_boot_id", failing)
    with pytest.raises(RuntimeError, match="cannot read the native boot identity"):
        _REAL_BOOT_ID()


def test_pitr_rollback_retires_through_the_recorded_adapter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class RetiredError(Exception):
        pass

    def darwin(record: dict[str, Any]) -> None:
        raise RetiredError(record["kind"])

    def linux_only(_record: dict[str, Any]) -> None:
        pytest.fail("a recorded darwin launch must not retire through the Linux adapter")

    monkeypatch.setattr(macos, "retire_current", darwin)
    monkeypatch.setattr(linux, "retire_current", linux_only)
    operation = journal.Operation.model_construct(
        request=PitrRequest.model_construct(),
        launch_attempted=True,
        launch={"kind": native.DARWIN},
    )
    with pytest.raises(RetiredError, match=native.DARWIN):
        pitr_submit._rollback(operation)


def test_executor_and_helper_births_close_only_as_recorded(harness: Harness) -> None:
    """The recorded births themselves must be gone; an empty group alone is not enough."""
    harness.record_native(harness.launched())
    harness.terminal(signal=9)
    harness.alive.add(EXECUTOR_BIRTH)
    with pytest.raises(RuntimeError, match="recorded executor still alive"):
        macos.readback(harness.plan)
