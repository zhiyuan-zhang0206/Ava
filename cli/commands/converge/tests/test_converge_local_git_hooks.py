"""Converge's warning-only assertion for local Git hook installations."""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

import cli.commands.converge._brew_pin as cbp
import cli.commands.converge._steps as csteps
import cli.commands.converge.host as cv
from base.config import ConfigBoot
from base.telemetry import EventPipeline
from tests.path_scoped.cli_tests import operator_database as operator_database
from tests.path_scoped.cli_tests import operator_pipeline as operator_pipeline


def _ctx(
    tmp_path: Path, *, operator_database: Callable[[], Any], producer: Callable[[], EventPipeline]
) -> cv.ConvergeCtx:
    return cv.ConvergeCtx(
        repo=Path("/repo"),
        ava_home=tmp_path,
        roles=cv.ALL_ROLES,
        config=ConfigBoot(),
        database_factory=operator_database,
        producer=producer,
    )


def _with_script(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    script = tmp_path / "scripts" / "provision" / "check_git_hooks.py"
    script.parent.mkdir(parents=True)
    script.write_text("# stub\n")

    def fake_repo_root() -> Path:
        return tmp_path

    monkeypatch.setattr(csteps, "repo_root", fake_repo_root)


def _fake_scan(monkeypatch: pytest.MonkeyPatch, stdout: str, code: int) -> None:
    def fake_run(args: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        assert args[1].endswith("check_git_hooks.py")
        assert args[2] == "--scan-machine"
        return subprocess.CompletedProcess(args, code, stdout=stdout, stderr="")

    monkeypatch.setattr(csteps.subprocess, "run", fake_run)


def test_step_is_registered_after_brew_pin() -> None:
    steps = cv.CONVERGE_STEPS
    hooks_index = next(
        i for i, step in enumerate(steps) if step.apply is csteps.ensure_local_git_hooks
    )
    brew_index = next(i for i, step in enumerate(steps) if step.apply is cbp.ensure_brew_pin)
    assert hooks_index > brew_index
    step = steps[hooks_index]
    assert step.roles == cv.ALL_ROLES
    assert step.requires_unit_config is False


def test_clean_machine_is_silent(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    operator_database: Callable[[], Any],
    operator_pipeline: Callable[[], EventPipeline],
) -> None:
    _with_script(monkeypatch, tmp_path)
    _fake_scan(monkeypatch, "hook check: OK (3 clone(s))\n", 0)

    csteps.ensure_local_git_hooks(
        _ctx(tmp_path, operator_database=operator_database, producer=operator_pipeline)
    )

    assert capsys.readouterr().err == ""


def test_drifted_machine_warns(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    operator_database: Callable[[], Any],
    operator_pipeline: Callable[[], EventPipeline],
) -> None:
    _with_script(monkeypatch, tmp_path)
    _fake_scan(
        monkeypatch,
        "WARNING: [/home/u/Ava] pre-commit: INSTALL_PYTHON points into a disposable worktree: /x\n"
        "hook check: 1 problem(s) across 1 clone(s)\n",
        1,
    )

    csteps.ensure_local_git_hooks(
        _ctx(tmp_path, operator_database=operator_database, producer=operator_pipeline)
    )

    err = capsys.readouterr().err
    assert err.startswith("  ! hooks: ")
    assert "disposable worktree" in err


def test_missing_script_is_silent(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    operator_database: Callable[[], Any],
    operator_pipeline: Callable[[], EventPipeline],
) -> None:
    def fake_repo_root() -> Path:
        return tmp_path

    monkeypatch.setattr(csteps, "repo_root", fake_repo_root)

    def fail(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        pytest.fail("the scan must not run without the script")

    monkeypatch.setattr(csteps.subprocess, "run", fail)

    csteps.ensure_local_git_hooks(
        _ctx(tmp_path, operator_database=operator_database, producer=operator_pipeline)
    )

    assert capsys.readouterr().err == ""


def test_usage_error_from_pre_flag_checkout_is_silent(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    operator_database: Callable[[], Any],
    operator_pipeline: Callable[[], EventPipeline],
) -> None:
    _with_script(monkeypatch, tmp_path)
    _fake_scan(monkeypatch, "usage: check_git_hooks.py [-h] [--scan-machine]\n", 2)

    csteps.ensure_local_git_hooks(
        _ctx(tmp_path, operator_database=operator_database, producer=operator_pipeline)
    )

    assert capsys.readouterr().err == ""


def test_subprocess_failure_is_silent(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    operator_database: Callable[[], Any],
    operator_pipeline: Callable[[], EventPipeline],
) -> None:
    _with_script(monkeypatch, tmp_path)

    def boom(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        raise OSError("git vanished")

    monkeypatch.setattr(csteps.subprocess, "run", boom)

    csteps.ensure_local_git_hooks(
        _ctx(tmp_path, operator_database=operator_database, producer=operator_pipeline)
    )

    assert capsys.readouterr().err == ""
