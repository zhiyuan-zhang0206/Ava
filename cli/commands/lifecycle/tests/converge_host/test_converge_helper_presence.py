"""A converge step that needs the permissions helper fails fast when its context carries none."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from base.telemetry import EventPipeline
from cli.commands.lifecycle import converge_host
from cli.commands.lifecycle.tests.converge_host.context_inputs import (
    accessibility_environment,
    capable_helper_context,
    screen_capture_environment,
)
from cli.database.tests.command_inputs import command_runtime as command_runtime
from tests.path_scoped.cli_tests import operator_database as operator_database
from tests.path_scoped.cli_tests import operator_pipeline as operator_pipeline

_BRING_UP = "permissions helper build + sign + load"


def _bring_up(ctx: converge_host.ConvergeCtx) -> None:
    (step,) = (step for step in converge_host.CONVERGE_STEPS if step.name == _BRING_UP)
    step.apply(ctx)


_STEPS = {
    "bring-up": _bring_up,
    "screen-capture": converge_host.ensure_screen_capture,
    "accessibility": converge_host.ensure_accessibility,
}


@pytest.mark.parametrize("step", sorted(_STEPS))
def test_a_host_that_can_run_the_helper_never_runs_without_one(
    step: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    operator_database: Callable[[], Any],
    operator_pipeline: Callable[[], EventPipeline],
) -> None:
    """None means only that a platform has no helper, so a capable host that arrives
    without one is a composition defect to stop on, not a reason to skip the step."""
    ctx = capable_helper_context(
        monkeypatch,
        tmp_path,
        operator_database=operator_database,
        producer=operator_pipeline,
        helper=None,
    )
    screen_capture_environment(monkeypatch, tmp_path, config=ctx.config)
    accessibility_environment(monkeypatch, tmp_path, config=ctx.config)
    with pytest.raises(RuntimeError, match="platform has none"):
        _STEPS[step](ctx)
