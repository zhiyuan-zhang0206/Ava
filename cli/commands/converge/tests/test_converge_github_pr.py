"""Converge github-PR step: fail fast when this host cannot open+merge memory PRs."""

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

import cli.commands.converge.host as cv
from base.config import ConfigBoot
from base.telemetry import EventPipeline
from tests.path_scoped.cli_tests import operator_database as operator_database
from tests.path_scoped.cli_tests import operator_pipeline as operator_pipeline


def _ctx(
    home: Path, *, operator_database: Callable[[], Any], producer: Callable[[], EventPipeline]
) -> cv.ConvergeCtx:
    return cv.ConvergeCtx(
        repo=Path("/repo"),
        ava_home=home,
        roles=frozenset({"agent-runner"}),
        config=ConfigBoot(),
        database_factory=operator_database,
        producer=producer,
    )


def test_passes_when_pr_capable(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    operator_database: Callable[[], Any],
    operator_pipeline: Callable[[], EventPipeline],
) -> None:
    import base.deploy.git.github_pr as gp

    monkeypatch.setattr(gp, "github_pr_blocker", lambda: None)
    cv._ensure_github_pr(
        _ctx(tmp_path, operator_database=operator_database, producer=operator_pipeline)
    )  # no raise


def test_raises_when_pr_blocked(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    operator_database: Callable[[], Any],
    operator_pipeline: Callable[[], EventPipeline],
) -> None:
    import base.deploy.git.github_pr as gp

    monkeypatch.setattr(gp, "github_pr_blocker", lambda: "gh CLI not installed")
    with pytest.raises(RuntimeError, match="gh CLI not installed"):
        cv._ensure_github_pr(
            _ctx(tmp_path, operator_database=operator_database, producer=operator_pipeline)
        )


def test_skips_on_single_box(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    operator_database: Callable[[], Any],
    operator_pipeline: Callable[[], EventPipeline],
) -> None:
    """A single box (also carries gateway) consolidates memory locally — skip the gate."""
    import base.deploy.git.github_pr as gp

    monkeypatch.setattr(gp, "github_pr_blocker", lambda: "gh CLI not installed")
    ctx = cv.ConvergeCtx(
        repo=Path("/repo"),
        ava_home=tmp_path,
        roles=frozenset({"gateway", "agent-runner"}),
        config=ConfigBoot(),
        database_factory=operator_database,
        producer=operator_pipeline,
    )
    cv._ensure_github_pr(ctx)  # no raise


def test_skips_when_opt_out(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    operator_database: Callable[[], Any],
    operator_pipeline: Callable[[], EventPipeline],
) -> None:
    """AVA_REQUIRE_GITHUB_PR=false opts a split runner out of the gate."""
    import base.deploy.git.github_pr as gp

    monkeypatch.setattr(gp, "github_pr_blocker", lambda: "gh CLI not installed")
    ctx = _ctx(tmp_path, operator_database=operator_database, producer=operator_pipeline)
    ctx.config.set_field("require_github_pr", False)
    cv._ensure_github_pr(ctx)  # no raise


def test_step_registered_agent_runner_only() -> None:
    step = next(s for s in cv.CONVERGE_STEPS if s.name == "github PR capability")
    assert step.roles == frozenset({"agent-runner"})
    assert step.requires_unit_config is True
