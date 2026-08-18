"""Converge github-PR step: fail fast when this host cannot open+merge memory PRs."""

from pathlib import Path

import pytest

import cli.commands._converge as cv


def _ctx(home: Path) -> cv.ConvergeCtx:
    return cv.ConvergeCtx(repo=Path("/repo"), ava_home=home, roles=frozenset({"agent-runner"}))


def test_passes_when_pr_capable(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import shared.github_pr as gp

    monkeypatch.setattr(gp, "github_pr_blocker", lambda: None)
    cv._ensure_github_pr(_ctx(tmp_path))  # no raise


def test_raises_when_pr_blocked(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import shared.github_pr as gp

    monkeypatch.setattr(gp, "github_pr_blocker", lambda: "gh CLI not installed")
    with pytest.raises(RuntimeError, match="gh CLI not installed"):
        cv._ensure_github_pr(_ctx(tmp_path))


def test_skips_on_single_box(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A single box (also carries gateway) consolidates memory locally — skip the gate."""
    import shared.github_pr as gp

    monkeypatch.setattr(gp, "github_pr_blocker", lambda: "gh CLI not installed")
    ctx = cv.ConvergeCtx(
        repo=Path("/repo"), ava_home=tmp_path, roles=frozenset({"gateway", "agent-runner"})
    )
    cv._ensure_github_pr(ctx)  # no raise


def test_skips_when_opt_out(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """AVA_REQUIRE_GITHUB_PR=false opts a split runner out of the gate."""
    import shared.github_pr as gp
    from shared.config import settings

    monkeypatch.setattr(gp, "github_pr_blocker", lambda: "gh CLI not installed")
    monkeypatch.setattr(settings.general, "require_github_pr", False)
    cv._ensure_github_pr(_ctx(tmp_path))  # no raise


def test_step_registered_agent_runner_only() -> None:
    step = next(s for s in cv.CONVERGE_STEPS if s.name == "github PR capability")
    assert step.roles == frozenset({"agent-runner"})
    assert step.requires_unit_config is True
