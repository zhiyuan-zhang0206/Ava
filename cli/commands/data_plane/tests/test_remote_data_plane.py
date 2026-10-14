"""Remote data-plane stop, status and leftover-local-instance diagnostics."""

import dataclasses
from collections.abc import Callable
from typing import Any

import pytest

from base.agents.context.clients import DatabaseFactory
from base.telemetry import EventPipeline
from cli.commands.data_plane import bringup as dp
from cli.commands.data_plane import cluster_instance as ci
from cli.commands.data_plane.config import DataPlaneConfig
from cli.commands.data_plane.tests.data_plane_records import remote_cluster_record
from tests.path_scoped.cli_tests import operator_database as operator_database
from tests.path_scoped.cli_tests import operator_pipeline as operator_pipeline

# ─── ava stop: nothing to tear down locally ──────────────────────────────────


def test_stop_remote_is_a_noop_with_message(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    operator_pipeline: Callable[[], EventPipeline],
    remote_data_plane_config: DataPlaneConfig,
) -> None:
    def _no_subprocess(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("no local subprocess may run against a remote data plane")

    monkeypatch.setattr(ci.subprocess, "run", _no_subprocess)

    rc = dp.stop_cluster_instance(remote_data_plane_config, producer=operator_pipeline)

    assert rc == 0
    out = capsys.readouterr().out
    assert "remote-managed" in out
    assert "nothing to stop locally" in out


# ─── ava status: probe the URLs, no local pooler line ────────────────────────


def test_status_remote_probes_urls_and_skips_pgbouncer_line(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    operator_database: Callable[[], Any],
    remote_data_plane_config: DataPlaneConfig,
) -> None:
    def _remote_postgres(
        config: DataPlaneConfig, *, database_factory: DatabaseFactory
    ) -> tuple[bool, str]:
        assert config is plane
        assert database_factory is operator_database
        return (True, "postgres (10.9.8.7:5432)")

    plane = dataclasses.replace(remote_data_plane_config, pgbouncer_enabled=True)
    monkeypatch.setattr(dp, "remote_pg_reachable", _remote_postgres)
    monkeypatch.setattr(
        dp,
        "remote_redis_reachable",
        lambda _config: (True, "redis (10.9.8.7:6380)"),  # pyright: ignore[reportUnknownArgumentType]
    )

    dp.print_data_plane_status(plane, database_factory=operator_database)

    out = capsys.readouterr().out
    assert "remote-managed" in out
    assert "✓ postgres (10.9.8.7:5432)" in out
    assert "✓ redis (10.9.8.7:6380)" in out
    assert "pgbouncer" not in out, "a remote plane has no local pooler to display"


def test_status_remote_reports_unreachable_component(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    operator_database: Callable[[], Any],
    remote_data_plane_config: DataPlaneConfig,
) -> None:
    def _remote_postgres(
        _config: DataPlaneConfig, *, database_factory: DatabaseFactory
    ) -> tuple[bool, str]:
        assert database_factory is operator_database
        return (True, "postgres (10.9.8.7:5432)")

    monkeypatch.setattr(dp, "remote_pg_reachable", _remote_postgres)
    monkeypatch.setattr(
        dp,
        "remote_redis_reachable",
        lambda _config: (False, "redis (10.9.8.7:6380) connect failed: timeout"),  # pyright: ignore[reportUnknownArgumentType]
    )

    dp.print_data_plane_status(remote_data_plane_config, database_factory=operator_database)

    out = capsys.readouterr().out
    assert "✗ redis (10.9.8.7:6380) connect failed: timeout" in out


def test_stop_remote_warns_about_orphaned_local_instance(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    operator_pipeline: Callable[[], EventPipeline],
    remote_data_plane_config: DataPlaneConfig,
) -> None:
    """A cluster that switched local→remote may still have its old local
    instance running; `ava stop` no longer manages it, so it must print a
    manual-teardown hint instead of silently leaving it (QA P2)."""
    from base import cluster

    rec = remote_cluster_record()
    monkeypatch.setattr(cluster, "get_record", lambda _home: rec)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(ci, "pg_running", lambda *_a: True)  # pyright: ignore[reportUnknownArgumentType]
    # The remote-managed home carries no local Redis admin password, so the
    # leftover Redis is detected by its listener, never by an authenticated PING.
    plane = dataclasses.replace(remote_data_plane_config, redis_admin_password="")
    monkeypatch.setattr(dp, "local_listener", lambda port: port == 18012)  # pyright: ignore[reportUnknownArgumentType]

    rc = dp.stop_cluster_instance(plane, producer=operator_pipeline)

    assert rc == 0
    captured = capsys.readouterr()
    combined = captured.out + captured.err
    assert "remote-managed" in combined
    assert "local postgres + redis from before the switch is still running" in combined
    assert "no longer managed" in combined
