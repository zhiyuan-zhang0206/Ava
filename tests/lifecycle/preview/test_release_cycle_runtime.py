"""Native completion, steady boot and durable completed-work proof boundaries."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, NoReturn

import pytest

from cli.release_transition import root_service
from cli.release_transition.journal import Operation, Retirement
from cli.release_transition.launcher_linux import LinuxJob
from cli.release_transition.native import LINUX
from cli.release_transition.request import ReleaseRef, Request
from scripts.preview import release_cycle_runtime as runtime
from scripts.preview import release_cycle_state as state
from shared.native_process.ownership import OwnedProcess
from shared.os_boot_unit import BootStartAction, BootUnitContext
from shared.runtime_release import VerifiedRelease
from tests.lifecycle.transition.test_journal import request_record as request_record


def _native(*, live: bool = False, failed: bool = False) -> LinuxJob:
    return LinuxJob(
        unit="ava-update.fixture.a0.service",
        boot_id="boot",
        invocation_id="a" * 32,
        cgroup="/system.slice/ava-update.fixture.a0.service",
        owner=OwnedProcess(900, 1.0, 20) if live else None,
        active="active",
        sub="running" if live else "exited",
        result="exit-code" if failed else "success",
        exit_code=0 if live else 1,
        exit_status=1 if failed else 0,
    )


@pytest.mark.parametrize("change", [None, "live", "rollback", "incomplete", "error", "failed"])
def test_only_closed_successful_requested_transition_counts_as_complete(
    request_record: Request, change: str | None
) -> None:
    operation = Operation(
        request=request_record,
        phase="observing" if change == "incomplete" else "complete",
        direction="previous" if change == "rollback" else "candidate",
        error="retained error" if change == "error" else None,
    )
    native = _native(live=change == "live", failed=change == "failed")
    if change in {"rollback", "incomplete", "error", "failed"}:
        with pytest.raises(RuntimeError):
            runtime._completed(operation, native, cleanup=False)
    else:
        assert runtime._completed(operation, native, cleanup=False) is (change is None)


def test_already_retired_previous_operation_never_reacquires_mutation_authority(
    request_record: Request, monkeypatch: pytest.MonkeyPatch
) -> None:
    native = _native()
    operation = Operation(
        request=request_record,
        phase="complete",
        launch={"kind": LINUX, "inert": True},
        launch_attempted=True,
        retirement=Retirement(terminal=native.model_dump(mode="json"), state="absent"),
    )

    def refuse(_record: object) -> NoReturn:
        pytest.fail("old operation cannot regain active authority")

    monkeypatch.setattr(runtime, "retire_current", refuse)
    assert runtime._completed(operation, native, cleanup=True)


@pytest.mark.parametrize("wrong", [False, True])
def test_steady_boot_uses_verified_pinned_image_in_existing_home_unit(
    request_record: Request, monkeypatch: pytest.MonkeyPatch, *, wrong: bool
) -> None:
    home = Path(request_record.home)
    root = home / "releases" / request_record.previous.artifact_digest
    image = VerifiedRelease(
        request_record.previous.artifact_digest,
        request_record.previous.manifest_digest,
        root,
        root / "venv/bin/python",
        root / "site",
    )
    other = VerifiedRelease("9" * 64, image.manifest_digest, root, image.interpreter, image.cwd)

    def verify(*_args: object) -> VerifiedRelease:
        return other if wrong else image

    monkeypatch.setattr(ReleaseRef, "verify", verify)
    calls: list[tuple[BootUnitContext, BootStartAction]] = []

    def install(*, context: BootUnitContext, action: BootStartAction) -> None:
        calls.append((context, action))

    monkeypatch.setattr(root_service, "install", install)
    if wrong:
        with pytest.raises(ValueError, match="differs"):
            root_service.install_steady(
                home, Path(request_record.registry), request_record.previous, image
            )
        assert not calls
    else:
        root_service.install_steady(
            home, Path(request_record.registry), request_record.previous, image
        )
        context, action = calls[0]
        assert context.home == home and context.registry == Path(request_record.registry)
        assert action.argv[:7] == (*image.module_argv("cli.release_transition.boot"),)
        assert action.cwd == image.cwd and "--operation" not in action.argv
        assert (
            "--artifact" in action.argv and request_record.previous.artifact_digest in action.argv
        )
        assert dict(action.environment)["AVA_HOME"] == request_record.home
        assert "PYTHONPATH" not in dict(action.environment)


def test_retained_agent_checkpoint_change_cannot_be_hidden_by_same_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    previous = {"agent": 5, "sha256": "before", "rows": {"checkpoints": 1}}
    (tmp_path / "release-frozen-a.json").write_text(
        json.dumps({"result": "passed", "agent": 5, "state": previous})
    )

    def changed(_run: Path, _agent: int) -> dict[str, Any]:
        return previous | {"sha256": "after"}

    monkeypatch.setattr(state, "state", changed)
    with pytest.raises(RuntimeError, match="identity/checkpoints changed"):
        state.verify(tmp_path, "b")
    evidence = json.loads((tmp_path / "release-state-b.json").read_text())
    assert evidence["result"] == "failed" and evidence["agents"][0]["sha256"] == "after"


def test_termination_acceptance_never_substitutes_for_native_closure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import httpx

    from cli.commands import _maintenance_stop

    (tmp_path / "smoke-release-a.json").write_text('{"agent": 5}')
    (tmp_path / "config.json").write_text(
        json.dumps({"gateway_url": "http://127.0.0.1:5010", "ports": {"gateway": 5010}})
    )
    calls: list[dict[str, Any]] = []

    def accepted(url: str, **kwargs: Any) -> httpx.Response:
        calls.append(kwargs)
        return httpx.Response(
            200, json={"status": "enqueued", "closed": True}, request=httpx.Request("POST", url)
        )

    def retained() -> None:
        raise RuntimeError("native execution resource remains")

    clock = iter((0.0, 100.0))
    monkeypatch.setattr(state.httpx, "post", accepted)

    def closed(_run: Path, _agent: int) -> dict[str, Any]:
        return {"agent": 5, "sha256": "closed-row"}

    monkeypatch.setattr(state, "state", closed)
    monkeypatch.setattr(state.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(_maintenance_stop, "require_no_terminals", retained)
    with pytest.raises(TimeoutError, match="native closure"):
        state.freeze(tmp_path, "a")
    assert len(calls) == 1 and calls[0]["json"] == {"force": False, "final": True}
    evidence = json.loads((tmp_path / "release-frozen-a.json").read_text())
    assert evidence["result"] == "failed" and "native execution" in evidence["pending"]


def _captured_request(request: Request, label: str = "ab") -> Path:
    import hashlib

    run = Path(request.home).parent
    path = run / f"release-{label}-request.json"
    path.write_text(request.model_dump_json() + "\n")
    (run / "release-inputs.json").write_text(
        json.dumps({"requests": {label: {"sha256": hashlib.sha256(path.read_bytes()).hexdigest()}}})
    )
    return run


def test_dispatch_reverifies_image_and_invokes_only_public_cli_with_clean_environment(
    request_record: Request, monkeypatch: pytest.MonkeyPatch
) -> None:
    import subprocess

    run = _captured_request(request_record)
    root = Path(request_record.home) / "releases" / request_record.candidate.artifact_digest
    image = VerifiedRelease(
        request_record.candidate.artifact_digest,
        request_record.candidate.manifest_digest,
        root,
        root / "venv/bin/python",
        root / "site",
    )
    events: list[str] = []

    def verified(_run: Path, name: str) -> tuple[ReleaseRef, VerifiedRelease]:
        assert name == "b"
        events.append("verify")
        return request_record.candidate, image

    def command(argv: tuple[str, ...], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        events.append("public CLI")
        assert argv == image.module_argv(
            "cli.main", "cluster", "update", "--prepared", str(run / "release-ab-request.json")
        )
        assert kwargs["cwd"] == image.cwd and kwargs["check"]
        assert kwargs["env"]["AVA_HOME"] == request_record.home
        assert kwargs["env"]["AVA_CLUSTER_REGISTRY"] == request_record.registry
        assert not {"PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV", "OPENAI_API_KEY"}.intersection(
            kwargs["env"]
        )
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(runtime, "image_input", verified)
    monkeypatch.setattr(runtime.subprocess, "run", command)
    runtime.dispatch(run, "ab")
    assert events == ["verify", "public CLI"]
    (run / "release-ab-request.json").write_text(
        request_record.model_copy(update={"machine": "changed"}).model_dump_json()
    )
    with pytest.raises(RuntimeError, match="request changed"):
        runtime.dispatch(run, "ab")
    assert events == ["verify", "public CLI", "verify"]


def test_fixture_runs_isolated_before_trusting_its_installed_origin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import hashlib
    import subprocess

    image = VerifiedRelease(
        "a" * 64,
        "b" * 64,
        tmp_path / "image",
        tmp_path / "image/venv/bin/python",
        tmp_path / "image/site",
    )
    path = tmp_path / "source/tests/e2e/fakes/scenarios/message_flow.py"
    path.parent.mkdir(parents=True)
    path.write_text("source scenario")
    observed = {
        "files": {
            "tests.e2e.fakes.scenarios.message_flow": hashlib.sha256(path.read_bytes()).hexdigest()
        },
        "reply_sha256": "reply",
    }
    calls: list[list[str]] = []

    def command(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        assert argv[:6] == [str(image.interpreter), "-I", "-B", "-X", "utf8", "-c"]
        assert "is_relative_to(root)" in argv[6]
        assert "model.invoke([])" in argv[6] and "print(1 + 2)" in argv[6]
        assert kwargs["cwd"] == image.cwd and "PYTHONPATH" not in kwargs["env"]
        return subprocess.CompletedProcess(argv, 0, json.dumps(observed), "")

    monkeypatch.setattr(runtime.subprocess, "run", command)
    assert runtime._fixture(tmp_path, image) == observed
    path.write_text("different scenario")
    with pytest.raises(RuntimeError, match="scripted fixtures differ"):
        runtime._fixture(tmp_path, image)
    assert len(calls) == 2


@pytest.mark.parametrize("changed_attempt", [False, True])
def test_executor_finishing_between_journal_and_native_reads_uses_final_same_attempt(
    request_record: Request, monkeypatch: pytest.MonkeyPatch, *, changed_attempt: bool
) -> None:
    before = Operation(
        request=request_record,
        phase="resuming",
        launch={"kind": LINUX, "unit": "same"},
        launch_attempted=True,
    )
    after = before.model_copy(
        update={
            "phase": "complete",
            "launch": {"kind": LINUX, "unit": "other"} if changed_attempt else before.launch,
        }
    )
    snapshots = iter((before, after))

    def read(_path: Path) -> Operation:
        return next(snapshots)

    def native(_record: object) -> LinuxJob:
        return _native()

    monkeypatch.setattr(runtime, "read_operation", read)
    monkeypatch.setattr(runtime, "readback", native)
    if changed_attempt:
        with pytest.raises(RuntimeError, match="identity changed"):
            runtime._sample(request_record, cleanup=False)
    else:
        observed, job = runtime._sample(request_record, cleanup=False)
        assert observed == after and job is not None
        assert runtime._completed(observed, job, cleanup=False)


@pytest.mark.parametrize("outcome", ["closed", "survivor", "unknown"])
def test_prior_generation_closure_records_every_captured_native_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, outcome: str
) -> None:
    from dataclasses import asdict

    import psutil

    from scripts.preview import release_cycle_custody as custody

    root = OwnedProcess(800, 1.0, 1)
    unit = OwnedProcess(801, 2.0, 2)
    descendant = OwnedProcess(802, 3.0, 3)
    (tmp_path / "cycle-release-a.json").write_text(
        json.dumps({"result": "passed", "births": {"root": asdict(root), "unit": asdict(unit)}})
    )

    def tree(_root: OwnedProcess) -> set[OwnedProcess]:
        return {root, unit, descendant}

    def alive(_owner: OwnedProcess) -> bool:
        return True

    monkeypatch.setattr(custody, "capture_tree", tree)
    monkeypatch.setattr(OwnedProcess, "live", alive)
    custody.capture(tmp_path, "a")

    def after(owner: OwnedProcess) -> bool:
        if outcome == "unknown" and owner == descendant:
            raise psutil.AccessDenied(owner.pid)
        return outcome == "survivor" and owner == descendant

    monkeypatch.setattr(OwnedProcess, "live", after)
    if outcome == "closed":
        custody.closed(tmp_path, "a")
    else:
        with pytest.raises((RuntimeError, psutil.AccessDenied)):
            custody.closed(tmp_path, "a")
    evidence = json.loads((tmp_path / "release-apps-a-closed.json").read_text())
    assert evidence["result"] == ("passed" if outcome == "closed" else "failed")
    if outcome != "unknown":
        assert len(evidence["observations"]) == 3
