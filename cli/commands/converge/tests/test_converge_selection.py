"""Preparation follows the desired roster without downloading unrelated services."""

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from base.telemetry import EventPipeline
from cli.commands.converge import host as converge_host
from cli.commands.converge.spec import ConvergeCtx, ConvergeStep
from cli.commands.converge.tests.context_inputs import Converge, default_home, other_home
from cli.commands.converge.tests.context_inputs import converge as converge
from cli.commands.converge.tests.context_inputs import home as home
from tests.path_scoped.cli_tests import operator_database as operator_database
from tests.path_scoped.cli_tests import operator_pipeline as operator_pipeline


def test_preparation_filters_service_consumers_but_keeps_shared_host_steps(
    tmp_path: Path,
    operator_database: Callable[[], Any],
    operator_pipeline: Callable[[], EventPipeline],
) -> None:
    calls: list[str] = []

    def shared(ctx: ConvergeCtx) -> None:
        assert ctx.services == frozenset({"loki"})
        calls.append("shared")

    def backend(_ctx: ConvergeCtx) -> None:
        calls.append("backend")

    def unrelated(_ctx: ConvergeCtx) -> None:
        pytest.fail("Unselected collector must not prepare assets")

    steps = (
        ConvergeStep("shared", shared),
        ConvergeStep("backend", backend, services=frozenset({"loki", "grafana"})),
        ConvergeStep("collector", unrelated, services=frozenset({"otel-collector"})),
    )
    converge_host.converge_host(
        tmp_path,
        frozenset({"gateway"}),
        ava_home=tmp_path / "home",
        steps=steps,
        services=frozenset({"loki"}),
        database_factory=operator_database,
        producer=operator_pipeline,
    )
    assert calls == ["shared", "backend"]


def test_converge_host_runs_universal_and_skips_unit_state_when_role_none(
    converge: Converge, home: Path, tmp_path: Path
):
    calls: list[str] = []
    steps = (
        converge_host.ConvergeStep("wiring", lambda _: calls.append("wiring")),
        converge_host.ConvergeStep(
            "unit", lambda _: calls.append("unit"), requires_unit_config=True
        ),
    )
    converge(tmp_path, None, ava_home=home, steps=steps)
    assert calls == ["wiring"]  # unit-state deferred when role is None


def test_converge_host_filters_by_role(converge: Converge, home: Path, tmp_path: Path):
    calls: list[str] = []
    steps = (
        converge_host.ConvergeStep(
            "cp-only",
            lambda _: calls.append("cp"),
            roles=frozenset({"gateway"}),
        ),
        converge_host.ConvergeStep("both", lambda _: calls.append("both")),
    )
    converge(tmp_path, frozenset({"agent-runner"}), ava_home=home, steps=steps)
    assert calls == ["both"]  # gateway-only step skipped on agent-runner


def test_converge_host_skips_host_global_for_dev_cluster(
    converge: Converge, home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Dev clusters must leave the production symlink and shell wiring alone."""
    monkeypatch.setattr(converge_host, "is_default_home", other_home)
    calls: list[str] = []
    steps = (
        converge_host.ConvergeStep(
            "hostwide", lambda _: calls.append("hostwide"), host_global=True
        ),
        converge_host.ConvergeStep("percluster", lambda _: calls.append("percluster")),
    )
    converge(tmp_path, frozenset({"gateway"}), ava_home=home, steps=steps)
    assert calls == ["percluster"]  # host-global skipped


def test_converge_host_runs_host_global_for_default_cluster(
    converge: Converge, home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """The prod default home (~/.ava, non-worktree repo) DOES run host-global wiring."""
    monkeypatch.setattr(converge_host, "is_default_home", default_home)
    calls: list[str] = []
    steps = (
        converge_host.ConvergeStep(
            "hostwide", lambda _: calls.append("hostwide"), host_global=True
        ),
        converge_host.ConvergeStep("percluster", lambda _: calls.append("percluster")),
    )
    converge(tmp_path, frozenset({"gateway"}), ava_home=home, steps=steps)
    assert calls == ["hostwide", "percluster"]


@pytest.mark.parametrize("worktree_parent", [".claude/worktrees", ".worktrees"])
def test_converge_host_skips_host_global_in_worktree_even_if_cluster_default(
    converge: Converge,
    home: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    worktree_parent,
):
    """An uninstalled worktree may resolve to ~/.ava; its repository still makes
    it a dev cluster and must never repoint the production symlink."""
    monkeypatch.setattr(converge_host, "is_default_home", default_home)
    wt_repo = tmp_path / worktree_parent / "feat-x"
    wt_repo.mkdir(parents=True)  # pyright: ignore[reportUnknownMemberType]
    calls: list[str] = []
    steps = (
        converge_host.ConvergeStep(
            "hostwide", lambda _: calls.append("hostwide"), host_global=True
        ),
        converge_host.ConvergeStep("percluster", lambda _: calls.append("percluster")),
    )
    converge(wt_repo, frozenset({"gateway"}), ava_home=home, steps=steps)
    assert calls == ["percluster"]  # host-global skipped despite cluster == default


def test_converge_host_fail_fast_reraises(converge: Converge, home: Path, tmp_path: Path):

    def boom(ctx: converge_host.ConvergeCtx):
        raise RuntimeError("nope")

    steps = (converge_host.ConvergeStep("boom", boom),)
    with pytest.raises(RuntimeError, match="nope"):
        converge(tmp_path, frozenset({"gateway"}), ava_home=home, steps=steps)


def test_converge_host_runs_in_order(converge: Converge, home: Path, tmp_path: Path):
    calls: list[str] = []
    steps = (
        converge_host.ConvergeStep("first", lambda _: calls.append("first")),
        converge_host.ConvergeStep("second", lambda _: calls.append("second")),
    )
    converge(tmp_path, frozenset({"gateway"}), ava_home=home, steps=steps)
    assert calls == ["first", "second"]
