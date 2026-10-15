from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

import cli.commands.lifecycle.converge_host as cv
from base.clock.tests.manual_timers import ManualTimers
from base.cluster.machine import MachineRoles
from base.cluster.machine.boot import machine_from_boot
from base.config import ConfigBoot
from base.host.system.hostport import Host
from base.host.system.tests.fake_commands import FakeCommands, Reply, fake_host
from base.sessions.tests.refuse_helper import refuse_helper
from base.telemetry import EventPipeline
from cli.database.tests.command_inputs import command_runtime as command_runtime
from tests.path_scoped.cli_tests import operator_database as operator_database
from tests.path_scoped.cli_tests import operator_pipeline as operator_pipeline


def _ctx(
    home: Path,
    *,
    operator_database: Callable[[], Any],
    producer: Callable[[], EventPipeline],
    host: Host,
    roles: MachineRoles = frozenset({"agent-runner"}),
) -> cv.ConvergeCtx:
    machine_config = ConfigBoot()
    return cv.ConvergeCtx(
        repo=Path("/repo"),
        ava_home=home,
        roles=roles,
        config=machine_config,
        database_factory=operator_database,
        producer=producer,
        machine=machine_from_boot(machine_config),
        helper=refuse_helper,
        host=host,
        services=frozenset(),
        timers=ManualTimers(),
    )


def test_raises_when_gh_is_not_authenticated(
    tmp_path: Path,
    operator_database: Callable[[], Any],
    operator_pipeline: Callable[[], EventPipeline],
) -> None:
    commands = FakeCommands()
    commands.on(["gh", "auth", "status"], Reply(returncode=1))
    ctx = _ctx(
        tmp_path,
        operator_database=operator_database,
        producer=operator_pipeline,
        host=fake_host(commands, programs={"gh": "/usr/bin/gh"}),
    )
    with pytest.raises(RuntimeError, match="not authenticated"):
        cv.ensure_github_pr(ctx)
    assert commands.argvs == [("gh", "auth", "status")]


def test_raises_when_pr_blocked(
    tmp_path: Path,
    operator_database: Callable[[], Any],
    operator_pipeline: Callable[[], EventPipeline],
) -> None:
    ctx = _ctx(
        tmp_path,
        operator_database=operator_database,
        producer=operator_pipeline,
        host=fake_host(),
    )
    with pytest.raises(RuntimeError, match="gh CLI not installed"):
        cv.ensure_github_pr(ctx)


def test_skips_on_single_box(
    tmp_path: Path,
    operator_database: Callable[[], Any],
    operator_pipeline: Callable[[], EventPipeline],
) -> None:
    commands = FakeCommands()
    ctx = _ctx(
        tmp_path,
        operator_database=operator_database,
        producer=operator_pipeline,
        host=fake_host(commands),
        roles=frozenset({"gateway", "agent-runner"}),
    )
    cv.ensure_github_pr(ctx)
    assert commands.calls == []


def test_skips_when_opt_out(
    tmp_path: Path,
    operator_database: Callable[[], Any],
    operator_pipeline: Callable[[], EventPipeline],
) -> None:
    ctx = _ctx(
        tmp_path,
        operator_database=operator_database,
        producer=operator_pipeline,
        host=fake_host(),
    )
    ctx.config.set_field("require_github_pr", False)
    cv.ensure_github_pr(ctx)


def test_step_registered_agent_runner_only() -> None:
    step = next(s for s in cv.CONVERGE_STEPS if s.name == "github PR capability")
    assert step.roles == frozenset({"agent-runner"})
    assert step.requires_unit_config is True
