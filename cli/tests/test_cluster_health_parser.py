"""The `ava cluster health` argument surface."""

from collections.abc import Callable

import pytest

from base.agents.context.clients import DatabaseFactory
from base.telemetry import EventPipeline
from cli.commands.cluster.tests.health_probe_inputs import (
    all_checks_pass as all_checks_pass,
)
from cli.commands.cluster.tests.health_probe_inputs import (
    provider_guard_healthy as provider_guard_healthy,
)
from cli.commands.cluster.tests.health_probe_inputs import (
    ran as ran,
)
from cli.commands.cluster.tests.health_probe_inputs import (
    signals as signals,
)
from cli.commands.cluster.tests.test_cluster_health import (
    _home as _home,
)
from cli.database.runtime import CommandRuntime
from cli.tests.bootstrap.command_inputs import command_runtime as command_runtime
from tests.path_scoped.cli_tests import operator_database as operator_database
from tests.path_scoped.cli_tests import operator_pipeline as operator_pipeline


def test_health_probe_dispatch_preserves_observation_options(
    monkeypatch: pytest.MonkeyPatch,
    operator_database: DatabaseFactory,
    operator_pipeline: Callable[[], EventPipeline],
    command_runtime: CommandRuntime,
) -> None:
    import cli.commands.cluster.health as _cluster_health_commands
    from cli.parsers import parse_args

    received: list[dict[str, object]] = []

    def probe(
        *,
        database_factory: DatabaseFactory,
        producer: Callable[[], EventPipeline],
        command: CommandRuntime,
        **kwargs: object,
    ) -> int:
        assert database_factory is operator_database
        assert producer is operator_pipeline
        assert command is command_runtime
        received.append(kwargs)
        return 1

    monkeypatch.setattr(_cluster_health_commands, "cmd_health_probe", probe)
    args = parse_args(["cluster", "health-probe", "--no-schema-check"])
    args.database_factory = operator_database
    args.producer = operator_pipeline
    args.runtime = command_runtime
    assert args.func(args) == 1
    assert received == [
        {
            "check_crash_loops": True,
            "check_schema": False,
        }
    ]


@pytest.mark.parametrize(
    ("flag", "destination"),
    [
        ("crash-loop-check", "crash_loop_check"),
        ("schema-check", "schema_check"),
    ],
)
def test_health_probe_check_flags_disable_only_when_negated(flag: str, destination: str) -> None:
    from cli.parsers import build_parser

    parser = build_parser()
    default = parser.parse_args(["cluster", "health-probe"])
    disabled = parser.parse_args(["cluster", "health-probe", f"--no-{flag}"])
    assert getattr(default, destination) is True
    assert getattr(disabled, destination) is False
    with pytest.raises(SystemExit) as refused:
        parser.parse_args(["cluster", "health-probe", f"--{flag}"])
    assert refused.value.code == 2
