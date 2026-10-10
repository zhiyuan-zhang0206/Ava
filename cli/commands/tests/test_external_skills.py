from __future__ import annotations

import importlib
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from base.telemetry import EventPipeline
from cli.commands.converge import host as converge_host
from tests.path_scoped.cli_tests import operator_database as operator_database
from tests.path_scoped.cli_tests import operator_pipeline as operator_pipeline

SKILL_NAME = "ava-guide"


@pytest.fixture
def home(tmp_path: Path) -> Path:
    host_home = tmp_path / "home"
    host_home.mkdir()
    return host_home


def _bridge_module():
    return importlib.import_module("cli.commands.extensions.external_skills")


def _write_source(repo: Path) -> None:
    for name in (SKILL_NAME,):
        source = repo / "ava_builtins" / "skills" / "platform" / name
        source.mkdir(parents=True)
        (source / "SKILL.md").write_text("operator guidance\n")


def _target(client_home: Path) -> Path:
    return client_home / "skills" / SKILL_NAME


def test_bridge_step_is_prod_host_global_and_skipped_for_dev_worktrees(
    home: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    operator_database: Callable[[], Any],
    operator_pipeline: Callable[[], EventPipeline],
) -> None:
    module = _bridge_module()
    step = next(
        candidate
        for candidate in converge_host.CONVERGE_STEPS
        if candidate.apply is module.converge_external_agent_skill
    )
    assert step.host_global
    assert not step.requires_unit_config
    assert step.roles == converge_host.ALL_ROLES
    (home / ".codex").mkdir()

    def default_home(_path: Path) -> bool:
        return True

    monkeypatch.setattr(converge_host, "is_default_home", default_home)
    monkeypatch.setattr(module.Path, "home", lambda: home)

    dev_repo = tmp_path / ".worktrees" / "feature"
    _write_source(dev_repo)
    converge_host.converge_host(
        dev_repo,
        None,
        ava_home=home / ".ava",
        steps=(step,),
        database_factory=operator_database,
        producer=operator_pipeline,
    )
    assert not _target(home / ".codex").exists()

    prod_repo = tmp_path / "prod" / "source"
    _write_source(prod_repo)
    (home / ".ava" / "configs").mkdir(parents=True)
    converge_host.converge_host(
        prod_repo,
        None,
        ava_home=home / ".ava",
        steps=(step,),
        database_factory=operator_database,
        producer=operator_pipeline,
    )
    assert _target(home / ".codex").is_dir()
