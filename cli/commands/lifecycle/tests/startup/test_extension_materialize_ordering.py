from __future__ import annotations

import inspect
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from base.cluster.machine import MachineIdentity
from base.telemetry import EventPipeline
from cli.commands.data_plane.config import DataPlaneConfig
from cli.database import OperatorDatabaseFactory
from cli.database.runtime import CommandRuntime
from cli.database.tests.command_inputs import command_runtime as command_runtime
from tests.path_scoped.cli_tests import operator_database as operator_database
from tests.path_scoped.cli_tests import operator_pipeline as operator_pipeline


def test_materialization_is_not_a_converge_step() -> None:
    from cli.commands.lifecycle.converge_host import CONVERGE_STEPS

    offenders = [s.name for s in CONVERGE_STEPS if "extension" in s.name.lower()]
    assert not offenders, (
        f"{offenders} is a converge STEP, but converge runs before this cluster's "
        "Postgres is started and before migrations apply. Call it from the start "
        "sequence after the schema check instead — see cli/commands/extensions/materialize.py:"
        "materialize_cluster_extensions."
    )


def _instrument_cold_start_seams(monkeypatch: pytest.MonkeyPatch, calls: list[str]) -> None:
    import cli.commands._repo as _repo_commands
    import cli.commands.lifecycle.start as _start_commands
    from cli.commands.data_plane import bringup
    from cli.commands.extensions import materialize
    from cli.commands.lifecycle import converge_host

    def skip_converge(*_args: object, **_kwargs: object) -> None:
        return None

    def ensure_gateway_data_plane(
        _config: DataPlaneConfig,
        *,
        retained_children: object,
        database_factory: Callable[[], Any],
        machine: MachineIdentity,
    ) -> int:
        return 0

    def prepare_gateway_schema(_config: DataPlaneConfig) -> None:
        return None

    def migrate(*, database_factory: Callable[[], Any], machine_name: Callable[[], str]) -> None:
        assert callable(machine_name)
        calls.append("migrate")

    def complete_gateway_data_plane(
        _config: DataPlaneConfig,
        *,
        refresh_schema: bool = True,
        database_factory: Callable[[], Any],
        machine: MachineIdentity,
    ) -> None:
        del refresh_schema

    def schema_check(_db_url: str) -> int:
        calls.append("schema-check")
        return 0

    def adopt(
        *, database_factory: Callable[[], Any], machine_name: Callable[[], str], skills_root: Path
    ) -> None:
        assert callable(machine_name)
        assert skills_root.name == "skills"
        calls.append("adopt")

    def record_materialize(*, database_factory: Callable[[], Any], skills_root: Path) -> None:
        assert skills_root.name == "skills"
        calls.append("materialize")

    monkeypatch.setattr(converge_host, "converge_host", skip_converge)
    monkeypatch.setattr(_start_commands, "ensure_gateway_data_plane", ensure_gateway_data_plane)
    monkeypatch.setattr(bringup, "prepare_gateway_schema", prepare_gateway_schema)
    monkeypatch.setattr(_start_commands, "cmd_migrations_apply", migrate)
    monkeypatch.setattr(bringup, "complete_gateway_data_plane", complete_gateway_data_plane)
    monkeypatch.setattr(_repo_commands, "assert_schema_current_or_die", schema_check)
    monkeypatch.setattr(materialize, "adopt_local_extensions", adopt)
    monkeypatch.setattr(materialize, "materialize_cluster_extensions", record_materialize)


def test_start_materializes_after_the_schema_check(
    monkeypatch: pytest.MonkeyPatch,
    operator_database: OperatorDatabaseFactory,
    operator_pipeline: Callable[[], EventPipeline],
    *,
    command_runtime: CommandRuntime,
) -> None:
    from cli.commands.lifecycle.start import prepare_cold_start

    calls: list[str] = []
    _instrument_cold_start_seams(monkeypatch, calls)

    rc = prepare_cold_start(
        Path("/repo"),
        frozenset({"gateway"}),
        (),
        database_factory=operator_database,
        producer=operator_pipeline,
        command=command_runtime,
    )

    assert rc == 0
    assert calls.index("migrate") < calls.index("materialize"), (
        "materialization must run AFTER pending migrations apply — otherwise the "
        "rollout that creates the extensions table reads it before it exists"
    )
    assert calls.index("schema-check") < calls.index("materialize"), (
        "materialization must run AFTER the schema-current check — reading the "
        "registry against a schema this checkout does not understand is exactly "
        "what that check exists to prevent"
    )


def test_standalone_converge_materializes_too() -> None:
    from cli.commands.lifecycle.converge_host import cmd_converge

    assert (
        "materialize_cluster_extensions(database_factory=database_factory, skills_root=skills_root)"
        in inspect.getsource(cmd_converge)
    )


def test_the_materializer_lives_beside_its_siblings() -> None:
    from cli.commands.extensions import materialize

    assert hasattr(materialize, "materialize_cluster_extensions")


def test_start_adopts_before_it_materializes(
    monkeypatch: pytest.MonkeyPatch,
    operator_database: OperatorDatabaseFactory,
    operator_pipeline: Callable[[], EventPipeline],
    *,
    command_runtime: CommandRuntime,
) -> None:
    from cli.commands.lifecycle.start import prepare_cold_start

    calls: list[str] = []
    _instrument_cold_start_seams(monkeypatch, calls)

    rc = prepare_cold_start(
        Path("/repo"),
        frozenset({"gateway"}),
        (),
        database_factory=operator_database,
        producer=operator_pipeline,
        command=command_runtime,
    )

    assert rc == 0
    assert calls.index("adopt") < calls.index("materialize")


def test_standalone_converge_adopts_too() -> None:
    from cli.commands.lifecycle.converge_host import cmd_converge

    source = inspect.getsource(cmd_converge)
    assert "adopt_local_extensions(" in source
    assert "machine_name=command.machine.name" in source
    assert "skills_root=skills_root" in source
