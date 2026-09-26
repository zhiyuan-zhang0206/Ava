"""One native dispatch point and kind-specific journal evidence for executor custody."""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from pydantic import JsonValue

from cli.release_transition import execute, journal, native, submit
from cli.release_transition import launcher_linux as linux
from cli.release_transition import launcher_macos as macos
from cli.release_transition.request import PitrRequest
from shared import os_boot_unit, paths
from shared.native_process.ownership import OwnedProcess
from tests.lifecycle.transition.macos.launchd_fake import Harness
from tests.lifecycle.transition.macos.launchd_fake import harness as harness


def test_recorded_launch_selects_its_own_adapter_kind() -> None:
    assert native.for_launch({"kind": native.LINUX}) is linux
    assert native.for_launch({"kind": native.DARWIN}) is macos
    with pytest.raises(ValueError, match="unknown native executor kind"):
        native.for_launch({"kind": "windows-job-v1"})
    with pytest.raises(KeyError):
        native.for_launch({"unit": "unlabelled"})


def test_host_adapter_has_no_fallback(monkeypatch: pytest.MonkeyPatch, harness: Harness) -> None:
    request = journal.read_operation(harness.path).request
    monkeypatch.setattr(os_boot_unit, "systemd_running", lambda: True)
    assert native.for_host(request) is linux
    monkeypatch.setattr(os_boot_unit, "systemd_running", lambda: False)
    monkeypatch.setattr(native, "_host_platform", lambda: "win32")
    with pytest.raises(RuntimeError, match="no fallback"):
        native.for_host(request)
    monkeypatch.setattr(native, "_host_platform", lambda: "darwin")
    with pytest.raises(RuntimeError, match="not connected"):
        native.for_host(request)


def test_darwin_submission_refuses_pitr_and_release_before_any_reservation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path.resolve() / "home"
    home.mkdir()
    monkeypatch.setattr(paths, "ava_home", lambda: home)
    monkeypatch.setattr(os_boot_unit, "systemd_running", lambda: False)
    monkeypatch.setattr(native, "_host_platform", lambda: "darwin")

    def unexpected(*_args: object, **_kwargs: object) -> None:
        pytest.fail("macOS admission must refuse before preflight or reservation")

    monkeypatch.setattr(submit, "LocalTransition", unexpected)
    monkeypatch.setattr(submit, "create", unexpected)
    pitr = PitrRequest.model_construct(id=uuid4(), home=str(home))
    with pytest.raises(ValueError, match="PITR is not admitted on macOS"):
        submit.submit_request(pitr)
    assert not (home / "updates").exists()


def test_executor_receipt_routes_by_recorded_kind(monkeypatch: pytest.MonkeyPatch) -> None:
    receipt: dict[str, JsonValue] = {"kind": native.DARWIN, "label": "exact"}
    monkeypatch.setattr(macos, "executor_receipt", lambda _record: receipt)
    assert execute._executor_receipt({"kind": native.DARWIN}) is receipt
    owner = OwnedProcess(os.getpid(), 1.5, 7)
    monkeypatch.setattr(OwnedProcess, "live", lambda _self: True)
    job = SimpleNamespace(
        unit="u.service", boot_id="b", invocation_id="i", cgroup="/c", owner=owner
    )
    monkeypatch.setattr(linux, "readback", lambda _record: job)
    assert execute._executor_receipt({"kind": native.LINUX})["pid"] == os.getpid()


def test_darwin_births_are_recorded_once_with_exact_equality(harness: Harness) -> None:
    job = harness.launched()
    harness.record_native(job)
    before = harness.path.read_bytes()
    harness.record_native(job)
    assert harness.path.read_bytes() == before
    assert job.executor is not None
    moved = job.executor.model_copy(update={"birth": job.executor.birth + 1e-6})
    with (
        journal.exclusive(harness.path) as current,
        pytest.raises(ValueError, match="executor identity changed"),
    ):
        drifted: dict[str, JsonValue] = {**job.identity, "executor": moved.model_dump(mode="json")}
        current.record_native(drifted)
    assert harness.path.read_bytes() == before


def _terminal(harness: Harness) -> dict[str, JsonValue]:
    harness.record_native(harness.launched())
    harness.terminal(exit_code=0)
    return macos.readback(harness.plan).model_dump(mode="json")


@pytest.mark.parametrize(
    "change",
    [
        {"kind": native.LINUX},
        {"state": "running"},
        {"runs": 2},
        {"helper": {"pid": 900, "birth": 1.5, "starttime": None}},
        {"executor": {"pid": 901, "birth": 2.5, "starttime": None}},
        {"label": "com.ava.release-executor.other.a0"},
        {"domain": "gui/0"},
        {"boot_id": "boot-z"},
        {"closed": None},
        {"closed": {"helper": {"pid": 900, "birth": 9.5, "starttime": None}}},
        {"asid": 1},
    ],
)
def test_darwin_retirement_requires_the_recorded_closed_job(
    harness: Harness, change: dict[str, JsonValue]
) -> None:
    terminal = _terminal(harness)
    with journal.exclusive(harness.path) as current:
        with pytest.raises(ValueError, match="executor"):
            current.request_retirement(terminal | change)
        assert current.request_retirement(terminal).retirement is not None
        with pytest.raises(ValueError, match="cannot change its closure evidence"):
            current.request_retirement(terminal | {"exit_code": 80})


def test_darwin_retirement_without_births_requires_no_closure_claim(harness: Harness) -> None:
    harness.launched()
    harness.terminal(exit_code=0)
    terminal = macos.readback(harness.plan).model_dump(mode="json")
    assert terminal["closed"] is None
    invented: dict[str, JsonValue] = {
        **terminal,
        "closed": {"helper": {"pid": 900, "birth": 1.5, "starttime": None}},
    }
    with journal.exclusive(harness.path) as current:
        with pytest.raises(ValueError, match="recorded executor identity"):
            current.request_retirement(invented)
        current.request_retirement(terminal)
