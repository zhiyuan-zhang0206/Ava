from __future__ import annotations

import pytest

from base.deploy.maintenance import admission, pause_owner
from base.deploy.tests.maintenance_factory import isolate as isolate
from cli.commands.lifecycle._pause_resume import resume_after_start
from cli.commands.lifecycle.tests.stop.stop_support import home as home
from cli.commands.lifecycle.tests.stop.stop_support import launch as launch
from cli.database import OperatorDatabaseFactory
from cli.database.runtime import CommandRuntime
from cli.database.tests.command_inputs import command_runtime as command_runtime
from cli.parsers import build_parser
from tests.path_scoped.cli_tests import operator_database as operator_database


def test_plain_start_and_parser_need_no_manual_operation(
    capsys: pytest.CaptureFixture[str],
    operator_database: OperatorDatabaseFactory,
    command_runtime: CommandRuntime,
) -> None:

    @resume_after_start
    def start(
        operation: pause_owner.PauseOwnerSnapshot | None, database_factory: OperatorDatabaseFactory
    ) -> int:
        assert operation is None
        assert not admission.held()
        return 0

    assert (
        start(None, operator_database, command_runtime.machine)
        == start(None, operator_database, command_runtime.machine)
        == 0
    )
    assert "hold released" not in capsys.readouterr().out
    parser = build_parser()
    stop = parser.parse_args(["stop", "--keep-infra", "--keep-service", "gateway", "--force"])
    assert stop.keep_infra and stop.keep_service == ["gateway"] and stop.force
    kept = parser.parse_args(["stop", "-y", "--keep-infra", "--keep-service", "frontend"])
    assert kept.keep_service == ["frontend"] and not kept.force and kept.timeout == 300


def test_pause_is_not_a_verb(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        build_parser().parse_args(["pause"])
    assert "invalid choice: 'pause'" in capsys.readouterr().err
