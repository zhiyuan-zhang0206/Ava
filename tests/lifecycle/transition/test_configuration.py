"""Retained release requests never authorize a later settings generation."""

from __future__ import annotations

import builtins
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest

from cli.release_transition import local, stage
from cli.release_transition.request import ReleaseRef, Request
from shared.runtime_release import ReleaseRejectedError
from shared.start_inputs import configuration_digest


@pytest.fixture
def request_fixture(tmp_path: Path) -> Request:
    home = tmp_path.resolve() / "home"
    home.mkdir()
    (home / ".env").write_text("AVA_MACHINE_NAME=original\n")
    (home / "service-selection.json").write_text('{"version":1,"mode":"only","names":[]}\n')
    previous = ReleaseRef(
        artifact_digest="a" * 64,
        manifest_digest="b" * 64,
        schema_digest="c" * 64,
        source_commit="d" * 40,
    )
    candidate = previous.model_copy(update={"artifact_digest": "e" * 64})
    return Request(
        id=uuid4(),
        home=str(home),
        registry=str(home.parent / "clusters.json"),
        created_at=datetime.now(UTC),
        platform_tag="Linux-test",
        machine="original",
        previous=previous,
        candidate=candidate,
        executor=candidate,
        configuration_digest=configuration_digest(home),
    )


def _change(request: Request, member: str) -> None:
    (Path(request.home) / member).write_text("changed after release preparation\n")


def _forbid_runtime_imports(monkeypatch: pytest.MonkeyPatch) -> None:
    original = builtins.__import__

    def guarded(name: str, *args: Any, **kwargs: Any) -> Any:
        if name in {
            "shared.config",
            "cli.start_runtime",
            "shared.os_boot_unit",
            "shared",
            "cli.commands._maintenance",
            "cli.commands._maintenance_stop",
            "cli.release_transition.root_service",
        }:
            raise AssertionError(f"changed configuration reached runtime import: {name}")
        return original(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded)


@pytest.mark.parametrize("member", [".env", "service-selection.json"])
@pytest.mark.parametrize("phase", ["prepared", "starting", "observing", "resuming"])
def test_stage_refuses_changed_inputs_before_settings_or_effects(
    request_fixture: Request,
    monkeypatch: pytest.MonkeyPatch,
    member: str,
    phase: str,
) -> None:
    request = request_fixture
    from cli.release_transition.journal import Operation

    operation = Operation.model_validate(
        {"phase": phase, "request": request, "direction": "candidate"}
    )

    def read(_path: Path) -> SimpleNamespace:
        return operation

    monkeypatch.setattr(stage, "read_operation", read)
    _change(request, member)
    _forbid_runtime_imports(monkeypatch)
    action = {
        "prepared": stage.preflight_operation,
        "starting": stage.start_operation,
        "observing": stage.observe_operation,
        "resuming": stage.observe_operation,
    }[phase]
    with pytest.raises(ReleaseRejectedError, match="configuration changed"):
        action(request.path)


@pytest.mark.parametrize("member", [".env", "service-selection.json"])
@pytest.mark.parametrize("action", ["preflight", "start", "observe", "resume"])
def test_executor_phase_refuses_drift_before_runtime_effects(
    request_fixture: Request,
    monkeypatch: pytest.MonkeyPatch,
    member: str,
    action: str,
) -> None:
    transition = object.__new__(local.LocalTransition)
    transition.request = request_fixture
    transition.home = Path(request_fixture.home)
    _change(request_fixture, member)
    _forbid_runtime_imports(monkeypatch)
    with pytest.raises(ReleaseRejectedError, match="configuration changed"):
        if action in {"start", "observe", "resume"}:
            getattr(transition, action)(SimpleNamespace(request=request_fixture))
        else:
            getattr(transition, action)()


def test_request_configuration_check_is_read_only_and_repeatable(request_fixture: Request) -> None:
    home = Path(request_fixture.home)
    before = {path: path.read_bytes() for path in home.rglob("*") if path.is_file()}
    request_fixture.require_configuration()
    Request.model_validate_json(request_fixture.model_dump_json()).require_configuration()
    assert {path: path.read_bytes() for path in home.rglob("*") if path.is_file()} == before


def test_configuration_identity_imports_without_settings(request_fixture: Request) -> None:
    code = """
import builtins
import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
original = builtins.__import__
def guarded(name, *args, **kwargs):
    if name == "shared.config" or name.startswith("shared.config.") or name.startswith("cli.commands"):
        raise AssertionError("configuration admission imported runtime settings: " + name)
    return original(name, *args, **kwargs)
builtins.__import__ = guarded
from shared.start_inputs import configuration_digest
from cli.release_transition.request import Request
assert configuration_digest(Path(sys.argv[2])) == sys.argv[3]
assert "shared.config" not in sys.modules
"""
    result = subprocess.run(  # noqa: S603 — isolated child reads this test's temporary home only
        [
            sys.executable,
            "-I",
            "-B",
            "-c",
            code,
            str(Path(__file__).resolve().parents[3]),
            request_fixture.home,
            request_fixture.configuration_digest,
        ],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert result.returncode == 0, result.stderr
