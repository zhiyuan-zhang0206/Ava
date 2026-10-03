"""Preparation follows the desired roster without downloading unrelated services."""

from pathlib import Path

import pytest

from cli.commands.converge import host as converge_host
from cli.commands.converge.spec import ConvergeCtx, ConvergeStep


def test_preparation_filters_service_consumers_but_keeps_shared_host_steps(tmp_path: Path) -> None:
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
    )
    assert calls == ["shared", "backend"]
