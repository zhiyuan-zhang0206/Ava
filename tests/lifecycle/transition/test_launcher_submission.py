"""Public resubmission crosses the real journal and Linux continuation adapter."""

from __future__ import annotations

import json
import subprocess
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from typing import NoReturn
from uuid import uuid4

import pytest
from pydantic import JsonValue

from cli.release_transition import journal, submit
from cli.release_transition import launcher_linux as linux
from cli.release_transition.request import Request
from shared import os_boot_unit, paths
from tests.lifecycle.transition.test_launcher_linux import (
    _closed_attempt,
    _readback_seams,
    _retiring_manager,
    _unexpected,
)
from tests.lifecycle.transition.test_launcher_linux import planned as planned


def _request_file(plan: dict[str, JsonValue], monkeypatch: pytest.MonkeyPatch) -> Path:
    operation = journal.read_operation(Path(str(plan["operation"])))
    request_path = operation.request.path.with_name("request.json")
    request_path.write_text(operation.request.model_dump_json())
    monkeypatch.setattr(paths, "ava_home", lambda: Path(operation.request.home))
    monkeypatch.setattr(os_boot_unit, "systemd_running", lambda: True)
    monkeypatch.setattr(submit, "LocalTransition", _unexpected)
    return request_path


def test_public_submission_loses_predecessor_while_waiting_for_home_lock_without_dispatch(
    planned: dict[str, JsonValue], monkeypatch: pytest.MonkeyPatch
) -> None:
    request_file = _request_file(planned, monkeypatch)
    old_path = Path(str(planned["operation"]))
    prior = journal.read_operation(old_path)
    request = prior.request.model_copy(update={"id": uuid4()})
    request_file.write_text(request.model_dump_json())
    store = Path(request.home) / "releases"
    events: list[str] = []

    class AdmittedInputs:
        def __init__(self, inputs: Request) -> None:
            self.candidate = inputs.executor.verify(Path(inputs.home), inputs.platform_tag)

        def preflight(self) -> None:
            assert journal.current_pointer(store) == request.previous.selector
            events.append("read-only preflight sees A")

    original_lock = journal.file_lock

    @contextmanager
    def previous_update_finishes(path: Path, *, timeout_s: float) -> Generator[None]:
        with original_lock(path, timeout_s=timeout_s):
            # Deterministically complete the earlier updater immediately before
            # the new request acquires mutation authority. No sleeps or threads.
            handle = journal.Journal(journal.read_operation(old_path))
            remaining: tuple[journal.Phase, ...] = (
                "quiescing",
                "stopping",
                "selecting",
                "starting",
                "observing",
                "resuming",
                "complete",
            )
            for phase in remaining:
                handle.advance(phase)
            (store / "current-release").write_text(
                json.dumps(
                    {
                        "artifact_digest": request.candidate.artifact_digest,
                        "manifest_digest": request.candidate.manifest_digest,
                    }
                )
            )
            events.append("prior update completes on B")
            yield

    monkeypatch.setattr(submit, "LocalTransition", AdmittedInputs)
    monkeypatch.setattr(journal, "file_lock", previous_update_finishes)
    monkeypatch.setattr(linux, "plan_launch", _unexpected)
    monkeypatch.setattr(linux, "launch", _unexpected)
    monkeypatch.setattr(linux, "_command", _unexpected)
    with pytest.raises(ValueError, match="predecessor is not the selected"):
        submit.submit(request_file)
    assert events == ["read-only preflight sees A", "prior update completes on B"]
    assert journal.read_operation(old_path).terminal
    assert (Path(request.home) / "updates/active").read_text().strip() == str(old_path)
    assert not request.path.parent.exists()


@pytest.mark.parametrize("crash", ["finished", "retired", "planned"])
@pytest.mark.parametrize("rollback", [False, True])
def test_public_submit_continues_closed_attempt_and_interrupted_relaunch(
    planned: dict[str, JsonValue], monkeypatch: pytest.MonkeyPatch, crash: str, *, rollback: bool
) -> None:
    _closed_attempt(planned, monkeypatch)
    request_file = _request_file(planned, monkeypatch)
    path = Path(str(planned["operation"]))
    terminal = linux.readback(planned)
    with journal.exclusive(path) as current:
        if rollback:
            current.advance("selecting")
            current.advance("starting")
            current.recover("candidate readiness failed")
        if crash != "finished":
            current.request_retirement(terminal.model_dump(mode="json"))
            current.record_retired()
            current.relaunch(terminal.model_dump(mode="json"))
        if crash == "planned":
            runtime = current.operation.request.executor.verify(
                Path(current.operation.request.home), current.operation.request.platform_tag
            )
            current.record_launch(linux.plan_launch(path, runtime))
    retire = _retiring_manager(planned, monkeypatch)
    calls: list[list[str]] = []

    def command(argv: list[str], *, privileged: bool = False) -> subprocess.CompletedProcess[str]:
        if retire(argv):
            return subprocess.CompletedProcess(argv, 0, "", "")
        current = journal.read_operation(path)
        assert privileged and current.launch is not None and current.launch_attempted
        assert current.attempt == 1 and len(current.retired_executors) == 1
        assert current.phase == "stopping"
        assert current.direction == ("previous" if rollback else "candidate")
        assert current.retired_executors[0]["terminal"] == terminal.model_dump(mode="json")
        assert current.launch["argv"] == planned["argv"]
        calls.append(argv)
        _readback_seams(monkeypatch, current.launch, pid=901)
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(linux, "_command", command)
    result_path, native = submit.submit(request_file)
    assert result_path == path
    assert native["owner"] == {"pid": 901, "birth": 1.5, "starttime": 150}
    assert len(calls) == 1
    monkeypatch.setattr(linux, "_command", _unexpected)
    assert submit.submit(request_file) == (result_path, native)


def test_public_submit_retained_children_refuses_without_relaunch(
    planned: dict[str, JsonValue], monkeypatch: pytest.MonkeyPatch
) -> None:
    _closed_attempt(planned, monkeypatch)
    request_file = _request_file(planned, monkeypatch)
    path = Path(str(planned["operation"]))
    before = path.read_bytes()

    def retained_children(*_args: object) -> NoReturn:
        raise RuntimeError("finished executor still owns native child processes")

    monkeypatch.setattr(linux, "_require_empty_cgroup", retained_children)
    monkeypatch.setattr(linux, "_command", _unexpected)
    with pytest.raises(RuntimeError, match="child processes"):
        submit.submit(request_file)
    assert path.read_bytes() == before


@pytest.mark.parametrize("reboot", [False, True])
def test_completed_public_submit_retires_once_and_replays_terminal_evidence(
    planned: dict[str, JsonValue], monkeypatch: pytest.MonkeyPatch, *, reboot: bool
) -> None:
    _closed_attempt(planned, monkeypatch)
    request_file = _request_file(planned, monkeypatch)
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
    retire = _retiring_manager(planned, monkeypatch)

    def command(argv: list[str], *, privileged: bool = False) -> subprocess.CompletedProcess[str]:
        assert retire(argv) and privileged
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(linux, "_command", command)
    first = submit.submit(request_file)
    before = path.read_bytes()
    if reboot:
        monkeypatch.setattr(linux, "_boot_id", lambda: "new-boot")
    monkeypatch.setattr(linux, "_command", _unexpected)
    monkeypatch.setattr(linux, "readback", _unexpected)
    assert submit.submit(request_file) == first
    assert path.read_bytes() == before
    assert journal.read_operation(path).attempt == 0


@pytest.mark.parametrize("absent", [False, True])
def test_public_submit_resumes_after_unit_deletion_and_controller_crash(
    planned: dict[str, JsonValue], monkeypatch: pytest.MonkeyPatch, *, absent: bool
) -> None:
    _closed_attempt(planned, monkeypatch)
    request_file = _request_file(planned, monkeypatch)
    path = Path(str(planned["operation"]))
    terminal = linux.readback(planned)
    with journal.exclusive(path) as current:
        current.request_retirement(terminal.model_dump(mode="json"))
        if absent:
            current.record_retired()

    def missing(_unit: str) -> dict[str, str]:
        return {"LoadState": "not-found"}

    monkeypatch.setattr(linux, "_properties", missing)

    def command(argv: list[str], *, privileged: bool = False) -> subprocess.CompletedProcess[str]:
        assert privileged and argv[0] == "/usr/bin/systemd-run"
        current = journal.read_operation(path)
        assert current.attempt == 1 and current.launch is not None
        assert current.retired_executors[0]["retirement"] == {
            "state": "absent",
            "terminal": terminal.model_dump(mode="json"),
        }
        _readback_seams(monkeypatch, current.launch, pid=901)
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(linux, "_command", command)
    result, native = submit.submit(request_file)
    assert result == path and native["owner"] == {"pid": 901, "birth": 1.5, "starttime": 150}


@pytest.mark.parametrize(("living", "reboot"), [(False, False), (True, False), (False, True)])
def test_new_public_request_retires_previous_before_replacing_active_pointer(
    planned: dict[str, JsonValue], monkeypatch: pytest.MonkeyPatch, *, living: bool, reboot: bool
) -> None:
    _closed_attempt(planned, monkeypatch)
    request_file = _request_file(planned, monkeypatch)
    old_path = Path(str(planned["operation"]))
    with journal.exclusive(old_path) as current:
        remaining: tuple[journal.Phase, ...] = (
            "selecting",
            "starting",
            "observing",
            "resuming",
            "complete",
        )
        for phase in remaining:
            current.advance(phase)
    if reboot:
        terminal = linux.readback(planned)
        with journal.exclusive(old_path) as current:
            current.request_retirement(terminal.model_dump(mode="json"))
            current.record_retired()
        monkeypatch.setattr(linux, "_boot_id", lambda: "new-boot")

        def absent(_unit: str) -> dict[str, str]:
            return {"LoadState": "not-found"}

        monkeypatch.setattr(linux, "_properties", absent)
    prior = journal.read_operation(old_path)
    request = prior.request.model_copy(update={"id": uuid4()})
    request_file.write_text(request.model_dump_json())

    class AdmittedInputs:
        def __init__(self, request: Request) -> None:
            self.candidate = request.executor.verify(Path(request.home), request.platform_tag)

        def preflight(self) -> None:
            pass

    monkeypatch.setattr(submit, "LocalTransition", AdmittedInputs)
    if living:
        _readback_seams(monkeypatch, planned)
    retire = _retiring_manager(planned, monkeypatch)

    def command(argv: list[str], *, privileged: bool = False) -> subprocess.CompletedProcess[str]:
        assert privileged and not living
        if retire(argv):
            return subprocess.CompletedProcess(argv, 0, "", "")
        old = journal.read_operation(old_path)
        assert old.retirement is not None and old.retirement.state == "absent"
        new = journal.read_operation(request.path)
        assert new.launch is not None and new.launch_attempted
        assert new.launch["boot_id"] == ("new-boot" if reboot else planned["boot_id"])
        _readback_seams(monkeypatch, new.launch, pid=901)
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(linux, "_command", command)
    active = Path(request.home) / "updates/active"
    if living:
        with pytest.raises(RuntimeError, match="not positively closed"):
            submit.submit(request_file)
        assert Path(active.read_text().strip()) == old_path and not request.path.exists()
        assert journal.read_operation(old_path) == prior
    else:
        assert submit.submit(request_file)[0] == request.path
        assert Path(active.read_text().strip()) == request.path
