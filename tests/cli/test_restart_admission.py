"""Restart admits its exact executable before any stop or lifecycle mutation."""

from __future__ import annotations

from pathlib import Path

import pytest

import cli.commands._repo as _repo_commands
import cli.commands._start_readiness_preflight as _start_readiness_preflight_commands
import cli.commands.start as _start_commands
import cli.commands.stop as _stop_commands
from cli.commands import _start_readiness_preflight, stop
from cli.start_runtime import StartRuntime, admit_loaded_release
from shared import lifecycle_status, release_operation, runtime_interpreter
from shared.runtime_release import ReleaseRejectedError, VerifiedRelease
from tests.cli.test_start_runtime import image as image


def _forbidden(*_args: object, **_kwargs: object) -> None:
    pytest.fail("inadmissible restart reached lifecycle effects")


@pytest.mark.parametrize(
    "problem", ["source", "unselected", "unisolated", "tampered", "foreign", "held", "source-owner"]
)
def test_restart_refuses_before_journal_preflight_or_stop(
    image: VerifiedRelease, monkeypatch: pytest.MonkeyPatch, problem: str
) -> None:

    home = image.root.parent.parent
    monkeypatch.setattr("shared.paths.ava_home", lambda: home)
    monkeypatch.setattr(runtime_interpreter, "WHEEL_RUNTIME", True)
    expected = "selected"
    if problem in {"source", "source-owner"}:
        monkeypatch.setattr(runtime_interpreter, "WHEEL_RUNTIME", False)
    if problem in {"unselected", "source-owner"}:
        (home / "releases/current-release").unlink()
    if problem == "source-owner":

        def foreign_source(_repo: Path) -> str:
            return "foreign source"

        monkeypatch.setattr("shared.paths.prod_service_checkout_error", foreign_source)
        expected = "foreign source"
    if problem == "unisolated":
        prefix, executable, package, _ = runtime_interpreter.loaded_runtime()
        monkeypatch.setattr(
            runtime_interpreter, "loaded_runtime", lambda: (prefix, executable, package, False)
        )
        expected = "isolated"
    if problem == "foreign":
        prefix, executable, package, isolated = runtime_interpreter.loaded_runtime()
        monkeypatch.setattr(
            runtime_interpreter,
            "loaded_runtime",
            lambda: (prefix.parent, executable, package, isolated),
        )
    if problem == "tampered":
        image.interpreter.write_bytes(b"changed interpreter")
        expected = "hash mismatch"
    if problem == "held":

        def held(_home: Path) -> None:
            raise RuntimeError("home operation holds startup")

        monkeypatch.setattr(release_operation, "require_start_authorized", held)
        expected = "holds startup"
    monkeypatch.setattr(lifecycle_status, "begin", _forbidden)
    monkeypatch.setattr(_repo_commands, "_preflight_probes", _forbidden)
    monkeypatch.setattr(_stop_commands, "_do_stop", _forbidden)
    monkeypatch.setattr(stop, "_release_self_heal_pause", _forbidden)
    with pytest.raises((ReleaseRejectedError, RuntimeError, ValueError), match=expected):
        stop.cmd_restart()


def test_installed_restart_carries_identical_admitted_runtime(
    image: VerifiedRelease, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = image.root.parent.parent
    monkeypatch.setattr("shared.paths.ava_home", lambda: home)
    monkeypatch.setattr(runtime_interpreter, "WHEEL_RUNTIME", True)
    monkeypatch.setattr("shared.proc.hosting_exec_domain", lambda: None)
    monkeypatch.setattr("shared.proc.hosting_supervised_session", lambda: None)
    monkeypatch.setattr(_repo_commands, "_preflight_probes", lambda: 0)
    calls: list[str] = []
    captured: list[StartRuntime] = []

    def preflight(repo: Path, *, check_launcher: bool, runtime: StartRuntime) -> int:
        assert repo == runtime.code_root and not check_launcher
        assert runtime.release == image
        captured.append(runtime)
        calls.append("preflight")
        return 0

    def stopped(_repo: Path, **_kwargs: object) -> int:
        calls.append("stop")
        return 0

    def started(*, persist_services: bool, runtime: StartRuntime) -> int:
        assert not persist_services and runtime is captured[0]
        runtime.validate(home)
        calls.append("start")
        return 0

    monkeypatch.setattr(_start_readiness_preflight_commands, "preflight_start_readiness", preflight)
    monkeypatch.setattr(_stop_commands, "_do_stop", stopped)
    monkeypatch.setattr(_start_commands, "_cmd_start_body", started)
    assert stop.cmd_restart() == 0
    assert calls == ["preflight", "stop", "start"]


def test_retained_preflight_uses_verified_interpreter_without_source_checks(
    image: VerifiedRelease, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = image.root.parent.parent
    monkeypatch.setattr("shared.paths.ava_home", lambda: home)
    runtime = admit_loaded_release(home)
    image.interpreter.chmod(0o500)
    monkeypatch.setattr(_start_readiness_preflight, "_machine_roles", lambda: None)
    for name in (
        "_prod_checkout_problem",
        "_migration_findings",
        "_venv_findings",
        "_private_tree_findings",
    ):
        monkeypatch.setattr(_start_readiness_preflight, name, _forbidden)
    assert (
        _start_readiness_preflight.preflight_start_readiness(
            runtime.code_root, check_launcher=False, runtime=runtime
        )
        == 0
    )
    image.interpreter.chmod(0o400)
    assert (
        _start_readiness_preflight.preflight_start_readiness(
            runtime.code_root, check_launcher=False, runtime=runtime
        )
        == 1
    )
