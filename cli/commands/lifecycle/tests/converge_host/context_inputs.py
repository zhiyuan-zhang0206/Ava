from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from functools import partial
from pathlib import Path
from typing import Any, cast

import pytest

from base.clock.tests.manual_timers import ManualTimers
from base.cluster.machine import MachineRoles
from base.cluster.machine.boot import machine_from_boot
from base.config import ConfigBoot, settings
from base.host.converge import accessibility, screen_capture
from base.host.permissions_helper.client import HelperFactory
from base.host.system import probes
from base.host.system.hostport import Host
from base.host.system.tests.fake_commands import DARWIN_ARM64, fake_host
from base.sessions.tests.refuse_helper import refuse_helper
from base.telemetry import EventPipeline
from cli.commands.lifecycle import converge_host
from cli.database.runtime import CommandRuntime
from cli.database.tests.command_inputs import command_runtime as command_runtime
from tests.path_scoped.cli_tests import operator_database as operator_database
from tests.path_scoped.cli_tests import operator_pipeline as operator_pipeline

__all__ = [
    "Converge",
    "accessibility_environment",
    "capable_helper_context",
    "converge",
    "converge_context",
    "default_home",
    "home",
    "other_home",
    "pgbouncer_context",
    "screen_capture_environment",
]

Converge = Callable[..., None]


@pytest.fixture
def converge(
    operator_database: Callable[[], Any],
    operator_pipeline: Callable[[], EventPipeline],
    command_runtime: CommandRuntime,
) -> Converge:
    return partial(
        converge_host.converge_host,
        database_factory=operator_database,
        producer=operator_pipeline,
        command=command_runtime,
    )


def converge_context(
    repo: Path,
    ava_home: Path,
    roles: MachineRoles | None = None,
    *,
    operator_database: Callable[[], Any],
    producer: Callable[[], EventPipeline],
    helper: HelperFactory | None = refuse_helper,
) -> converge_host.ConvergeCtx:
    config = ConfigBoot()
    machine = machine_from_boot(config)
    if roles is not None:
        machine.set(role=roles)
    return converge_host.ConvergeCtx(
        repo=repo,
        ava_home=ava_home,
        roles=roles,
        config=config,
        machine=machine,
        database_factory=operator_database,
        producer=producer,
        helper=helper,
        host=fake_host(),
        services=frozenset(),
        timers=ManualTimers(),
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
        (tmp_path / ".env").write_text(f"AVA_DB_URL={db_url}\n")
    return ctx


def capable_helper_context(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    operator_database: Callable[[], Any],
    producer: Callable[[], EventPipeline],
    helper: HelperFactory | None = refuse_helper,
) -> converge_host.ConvergeCtx:
    ava_home = tmp_path / "avahome"
    ava_home.mkdir()
    (ava_home / ".env").write_text("")

    def fake_permissions_helper_incapability(*, host: Host) -> str | None:
        return None

    monkeypatch.setattr(
        probes, "permissions_helper_incapability", fake_permissions_helper_incapability
    )
    ctx = replace(
        converge_context(
            tmp_path,
            ava_home,
            operator_database=operator_database,
            producer=producer,
            helper=helper,
        ),
        host=fake_host(platform=DARWIN_ARM64),
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
    monkeypatch.setattr(screen_capture, "ava_home", lambda: tmp_path)
    config.set_field("permissions_helper_enabled", enabled)

    def fake_permissions_helper_incapability(*, host: Host) -> str | None:
        return incapability

    monkeypatch.setattr(
        probes, "permissions_helper_incapability", fake_permissions_helper_incapability
    )


def accessibility_environment(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    config: ConfigBoot,
    enabled: bool = True,
    incapability: str | None = None,
) -> None:
    monkeypatch.setattr(accessibility, "ava_home", lambda: tmp_path)
    config.set_field("permissions_helper_enabled", enabled)

    def fake_permissions_helper_incapability(*, host: Host) -> str | None:
        return incapability

    monkeypatch.setattr(
        probes, "permissions_helper_incapability", fake_permissions_helper_incapability
    )


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("SHELL", "/bin/zsh")
    return tmp_path
