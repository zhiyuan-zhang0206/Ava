from __future__ import annotations

import dataclasses
from collections.abc import Callable

import pytest

from base import telemetry
from base.cluster import postgres as owned_postgres
from base.telemetry import EventPipeline
from cli.commands.data_plane import bringup as dp
from cli.commands.data_plane import cluster_instance as instance
from cli.commands.data_plane import pgbouncer
from cli.commands.data_plane.config import DataPlaneConfig
from cli.database.tests.command_inputs import command_runtime as command_runtime
from tests.path_scoped.cli_tests import operator_database as operator_database
from tests.path_scoped.cli_tests import operator_pipeline as operator_pipeline


def test_stop_cluster_instance_reports_an_escalation(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    operator_pipeline: Callable[[], EventPipeline],
    data_plane_config: DataPlaneConfig,
) -> None:
    escalation = owned_postgres.Escalation(
        detail="fast shutdown did not complete within 277s", killed=(4242,)
    )
    emitted: list[tuple[tuple[object, ...], dict[str, object]]] = []

    def fake_emit(*args: object, **kwargs: object) -> None:
        emitted.append((args, kwargs))

    def fake_stop(*args: object, **kwargs: object) -> owned_postgres.Escalation:
        return escalation

    def fake_pg_data_dir() -> None:
        return None

    def fake_pgbouncer_stop() -> None:
        return None

    monkeypatch.setattr(telemetry, "emit", fake_emit)
    monkeypatch.setattr(instance, "pg_data_dir", fake_pg_data_dir)
    monkeypatch.setattr(instance.owned_postgres, "stop", fake_stop)
    monkeypatch.setattr(pgbouncer, "stop_pgbouncer", fake_pgbouncer_stop)

    local_plane = dataclasses.replace(
        data_plane_config,
        db_url="postgresql://meiva@127.0.0.1/meiva",
        redis_url="redis://127.0.0.1",
    )

    assert dp.stop_cluster_instance(local_plane, producer=operator_pipeline) == 0

    assert len(emitted) == 1
    (args, kwargs) = emitted[0]
    assert args[1] == "postgres_stop_escalated"
    assert kwargs["level"] == "error"
    assert "ended by an immediate shutdown" in capsys.readouterr().err
