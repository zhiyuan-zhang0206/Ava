"""CLI composition owns admission without changing a service's posture."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from base.db import Database
from base.db.code_version_gate import ProcessDbGate
from base.telemetry import EventPipeline
from cli.database import OperatorDatabaseFactory, operator_database_factory
from tests.path_scoped.cli_tests import operator_database as operator_database
from tests.path_scoped.cli_tests import operator_pipeline as operator_pipeline


def test_operator_handles_share_the_command_gate_without_git(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[ProcessDbGate] = []

    def from_settings(*, gate: ProcessDbGate) -> Database:
        seen.append(gate)
        assert gate.application_name() == "ava:cli"
        assert not gate.min_read_due()
        return MagicMock(spec=Database)

    monkeypatch.setattr(Database, "from_settings", from_settings)
    factory = operator_database_factory()
    assert not seen
    factory()
    factory()
    assert len(seen) == 2
    assert seen[0] is seen[1]
    # Creating an operator never changes the independently supplied service gate.
    service = ProcessDbGate(version=lambda: 12, process="service")
    assert service.min_read_due()
    assert service.application_name() == "ava:service:v12"


def test_separate_commands_own_separate_exempt_gates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[ProcessDbGate] = []

    def from_settings(*, gate: ProcessDbGate) -> Database:
        seen.append(gate)
        return MagicMock(spec=Database)

    monkeypatch.setattr(Database, "from_settings", from_settings)
    operator_database_factory()()
    operator_database_factory()()
    assert seen[0] is not seen[1]


def test_each_operator_handle_reads_the_live_connection_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from base.config import settings

    monkeypatch.setattr(settings.data_plane, "pgbouncer_enabled", False)
    first_url = "postgresql://operator@127.0.0.1:6101/ava"
    second_url = "postgresql://operator@127.0.0.1:6102/ava"
    factory = operator_database_factory()
    monkeypatch.setattr(settings.data_plane, "db_url", first_url)
    first = factory()
    monkeypatch.setattr(settings.data_plane, "db_url", second_url)
    second = factory()
    assert first.direct_url() == first_url
    assert second.direct_url() == second_url


def test_operator_handle_preserves_the_original_constructor_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    error = RuntimeError("operator configuration failed")

    def fail(*, gate: ProcessDbGate) -> Database:
        assert not gate.min_read_due()
        raise error

    monkeypatch.setattr(Database, "from_settings", fail)
    with pytest.raises(RuntimeError) as caught:
        operator_database_factory()()
    assert caught.value is error


def test_command_pipeline_is_lazy_and_retains_the_same_database_factory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from base import telemetry
    from base.agents.context.clients import DatabaseFactory
    from cli.database import operator_event_pipeline

    factory = operator_database_factory()
    pipeline = MagicMock(spec=telemetry.EventPipeline)
    seen: list[DatabaseFactory] = []

    def build(*, database: DatabaseFactory) -> telemetry.EventPipeline:
        seen.append(database)
        return pipeline

    monkeypatch.setattr(telemetry, "build_pipeline", build)
    producer = operator_event_pipeline(factory)
    assert not seen
    assert producer() is pipeline
    assert producer() is pipeline
    assert seen == [factory]


def test_recovery_url_handle_keeps_the_operator_gate_and_live_slice(
    monkeypatch: pytest.MonkeyPatch,
) -> None:

    from base.config import settings
    from base.db.config import DbConfig

    seen: list[tuple[DbConfig, ProcessDbGate]] = []
    original_init = Database.__init__

    def initialize(
        self: Database,
        config: DbConfig,
        *,
        gate: ProcessDbGate | None = None,
        local_host: Callable[[], str] | None = None,
    ) -> None:
        assert gate is not None and not gate.min_read_due()
        seen.append((config, gate))
        original_init(self, config, gate=gate, local_host=local_host)

    monkeypatch.setattr(Database, "__init__", initialize)
    monkeypatch.setattr(settings.data_plane, "pgbouncer_enabled", False)
    live_url = "postgresql://operator@127.0.0.1:6101/ava"
    scratch_url = "postgresql://operator@127.0.0.1:6102/ava"
    monkeypatch.setattr(settings.data_plane, "db_url", live_url)
    factory = operator_database_factory()
    assert not seen
    regular = factory()
    monkeypatch.setattr(settings.data_plane, "db_pool_max_size", 7)
    scratch = factory.for_url(scratch_url)
    assert regular.direct_url() == live_url
    assert scratch.direct_url() == scratch_url
    assert seen[0][1] is seen[1][1]
    assert seen[1][0].db_pool_max_size == 7
    assert settings.data_plane.db_url == live_url


def test_borrowed_cli_context_keeps_the_original_operator_factory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    operator_database: OperatorDatabaseFactory,
    operator_pipeline: Callable[[], EventPipeline],
) -> None:
    from cli.commands.converge.tests.context_inputs import converge_context

    seen: list[ProcessDbGate] = []

    def from_settings(*, gate: ProcessDbGate) -> Database:
        seen.append(gate)
        return MagicMock(spec=Database)

    monkeypatch.setattr(Database, "from_settings", from_settings)
    context = converge_context(
        tmp_path, tmp_path, operator_database=operator_database, producer=operator_pipeline
    )
    assert isinstance(operator_database, OperatorDatabaseFactory)
    assert context.database_factory is operator_database
    assert not seen
    context.database_factory()
    operator_database()
    assert len(seen) == 2
    assert seen[0] is seen[1]
    assert not seen[0].min_read_due()
