"""Real converge context inputs and one explicit operator owner per invocation."""

from __future__ import annotations

from collections.abc import Callable
from functools import partial
from pathlib import Path
from typing import Any, cast

import pytest

from base.cluster.machine import MachineRoles
from base.config import ConfigBoot, settings
from base.telemetry import EventPipeline
from cli.commands.converge import host as converge_host
from tests.path_scoped.cli_tests import operator_database as operator_database
from tests.path_scoped.cli_tests import operator_pipeline as operator_pipeline

Converge = Callable[..., None]


@pytest.fixture
def converge(
    operator_database: Callable[[], Any], operator_pipeline: Callable[[], EventPipeline]
) -> Converge:
    return partial(
        converge_host.converge_host, database_factory=operator_database, producer=operator_pipeline
    )


def converge_context(
    repo: Path,
    ava_home: Path,
    roles: MachineRoles | None = None,
    *,
    operator_database: Callable[[], Any],
    producer: Callable[[], EventPipeline],
) -> converge_host.ConvergeCtx:
    return converge_host.ConvergeCtx(
        repo=repo,
        ava_home=ava_home,
        roles=roles,
        config=ConfigBoot(),
        database_factory=operator_database,
        producer=producer,
    )


def default_home(_home: Path) -> bool:
    return True


def other_home(_home: Path) -> bool:
    return False


def pgbouncer_context(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    db_url: str | None,
    enabled: bool,
    operator_database: Callable[[], Any],
    producer: Callable[[], EventPipeline],
) -> converge_host.ConvergeCtx:
    """Wire ensure_pgbouncer_step's deps: a record (pooler 6433 / pg 5433), settings
    reflecting the toggle, and an optional existing .env carrying the pre-cutover
    AVA_DB_URL."""
    from base import cluster

    rec = cluster.ClusterRecord(
        ports=cast(
            "cluster.ClusterPorts",
            {"gateway": 8000, "postgres": 5433, "redis": 6380, "pgbouncer": 6433},
        ),
        gateway_home=str(tmp_path),
        created_at="t",
    )

    def record(_home: Path) -> cluster.ClusterRecord:
        return rec

    monkeypatch.setattr(cluster, "get_record", record)
    monkeypatch.setattr(settings.data_plane, "pgbouncer_enabled", enabled)
    ctx = converge_context(
        tmp_path / "repo", tmp_path, operator_database=operator_database, producer=producer
    )
    if db_url is not None:
        (tmp_path / ".env").write_text(f"AVA_DB_URL={db_url}\nAVA_PGBOUNCER_PORT=6433\n")
    return ctx


def capable_helper_context(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    operator_database: Callable[[], Any],
    producer: Callable[[], EventPipeline],
) -> converge_host.ConvergeCtx:
    """A host the capability probe clears, with an empty .env."""
    ava_home = tmp_path / "avahome"
    ava_home.mkdir()
    (ava_home / ".env").write_text("")
    monkeypatch.setattr(converge_host.sys, "platform", "darwin")
    monkeypatch.setattr("base.host.system.probes.permissions_helper_incapability", lambda: None)
    ctx = converge_context(
        tmp_path, ava_home, operator_database=operator_database, producer=producer
    )
    ctx.config.set_field("permissions_helper_enabled", True)
    return ctx


def screen_capture_environment(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    config: ConfigBoot,
    enabled: bool = True,
    incapability: str | None = None,
) -> None:
    monkeypatch.setattr("base.host.converge.screen_capture.ava_home", lambda: tmp_path)
    config.set_field("permissions_helper_enabled", enabled)
    monkeypatch.setattr(
        "base.host.system.probes.permissions_helper_incapability", lambda: incapability
    )


def accessibility_environment(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    config: ConfigBoot,
    enabled: bool = True,
    incapability: str | None = None,
) -> None:
    monkeypatch.setattr("base.host.converge.accessibility.ava_home", lambda: tmp_path)
    config.set_field("permissions_helper_enabled", enabled)
    monkeypatch.setattr(
        "base.host.system.probes.permissions_helper_incapability", lambda: incapability
    )


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("SHELL", "/bin/zsh")
    return tmp_path
