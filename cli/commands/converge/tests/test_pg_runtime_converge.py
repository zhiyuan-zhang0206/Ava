"""The start converge step accepts an installed PostgreSQL 17 only where the platform has no vendored artifact, and a remote-managed gateway needs no local server or extension."""

from __future__ import annotations

import shlex
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

import base.config as config_facade
from base.agents.context.clients import DatabaseFactory
from base.cluster.dataplane import pg_runtime, runtime_binaries
from base.cluster.machine.boot import machine_from_boot
from base.config import ConfigBoot
from base.telemetry import EventPipeline
from cli.commands.converge.spec import ConvergeCtx
from cli.commands.converge.steps import ensure_pg_binaries_step
from cli.database.runtime import CommandRuntime
from cli.database.tests.command_inputs import command_runtime as command_runtime
from tests.path_scoped.cli_tests import operator_database as operator_database
from tests.path_scoped.cli_tests import operator_pipeline as operator_pipeline


@pytest.fixture
def installed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    bindir = tmp_path / "pg17" / "bin"
    bindir.mkdir(parents=True)
    extension = bindir.parent / "share" / "extension"
    extension.mkdir(parents=True)
    library = bindir.parent / "lib"
    library.mkdir()
    (extension / "vector.control").write_text("default_version = '0.8.6'\n")
    (extension / "vector--0.8.6.sql").write_text("-- fixture\n")
    suffix = ".dylib" if sys.platform == "darwin" else ".so"
    (library / f"vector{suffix}").touch()
    body = (
        '#!/bin/sh\ncase "$1" in\n'
        ' --version) echo "PostgreSQL 17.11";;\n'
        f" --bindir) echo {shlex.quote(str(bindir))};;\n"
        f" --sharedir) echo {shlex.quote(str(extension.parent))};;\n"
        f" --pkglibdir) echo {shlex.quote(str(library))};;\n"
        " *) exit 3;;\nesac\n"
    )
    for name in ("postgres", "initdb", "pg_ctl", "pg_config", "pg_dump", "pg_restore"):
        tool = bindir / name
        tool.write_text(body)
        tool.chmod(0o700)
    monkeypatch.setattr(runtime_binaries, "vendored_pg_bin_dir", lambda: None)
    monkeypatch.setattr(runtime_binaries, "vendored_pg_supported", lambda: False)

    def installed_tool(name: str) -> Path:
        return bindir / name

    def no_path_tool(_name: str) -> None:
        return None

    def as_method(_self: object, name: str) -> Path:
        return installed_tool(name)

    monkeypatch.setattr(type(pg_runtime.get_backend()), "pg_binary_path", as_method)
    monkeypatch.setattr(pg_runtime.shutil, "which", no_path_tool)

    def forbid_download() -> Path:
        pytest.fail("An installed runtime must not trigger vendored downloads")

    monkeypatch.setattr(runtime_binaries, "ensure_pg_binaries", forbid_download)
    monkeypatch.setattr(runtime_binaries, "ensure_pgvector", forbid_download)
    return bindir


def test_start_converge_accepts_installed_pg17_where_no_artifact_exists(
    installed: Path,
    operator_database: Callable[[], Any],
    operator_pipeline: Callable[[], EventPipeline],
    *,
    command_runtime: CommandRuntime,
) -> None:
    ensure_pg_binaries_step(
        ConvergeCtx(
            installed.parent,
            installed.parent,
            frozenset({"gateway"}),
            config=command_runtime.config,
            database_factory=operator_database,
            producer=operator_pipeline,
            machine=command_runtime.machine,
        )
    )
    assert pg_runtime.pg_tool("postgres") == installed / "postgres"


def test_remote_managed_gateway_does_not_require_local_server_or_extension(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    operator_database: Callable[[], Any],
    operator_pipeline: Callable[[], EventPipeline],
    *,
    command_runtime: CommandRuntime,
) -> None:
    def forbidden() -> None:
        pytest.fail("Remote-managed storage must not prepare a local server runtime")

    config = command_runtime.config
    config.set_field("db_url", "postgresql://test:test@192.0.2.10:5433/ava")
    config.set_field("redis_url", "redis://127.0.0.1:6380/0")
    monkeypatch.setattr(pg_runtime, "ensure_pg_runtime", forbidden)
    ensure_pg_binaries_step(
        ConvergeCtx(
            tmp_path,
            tmp_path,
            frozenset({"gateway"}),
            config=config,
            database_factory=operator_database,
            producer=operator_pipeline,
            machine=command_runtime.machine,
        )
    )


def test_pg_admission_uses_each_owners_live_remote_verdict(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    operator_database: DatabaseFactory,
    operator_pipeline: Callable[[], EventPipeline],
) -> None:
    calls: list[str] = []
    monkeypatch.setattr(pg_runtime, "ensure_pg_runtime", lambda: calls.append("local"))
    monkeypatch.setattr(
        config_facade.settings.data_plane, "db_url", "postgresql://ambient@192.0.2.20/other"
    )
    local, remote = ConfigBoot(), ConfigBoot()
    contexts: list[ConvergeCtx] = []
    for config, host in ((local, "127.0.0.1"), (remote, "192.0.2.10")):
        config.set_field("db_url", f"postgresql://test:test@{host}:5433/ava")
        config.set_field("redis_url", "redis://127.0.0.1:6380/0")
        contexts.append(
            ConvergeCtx(
                tmp_path,
                tmp_path,
                frozenset({"gateway"}),
                config=config,
                database_factory=operator_database,
                producer=operator_pipeline,
                machine=machine_from_boot(config),
            )
        )
    ensure_pg_binaries_step(contexts[0])
    ensure_pg_binaries_step(contexts[1])
    assert calls == ["local"]

    local.set_field("db_url", "postgresql://test:test@192.0.2.10:5433/ava")
    remote.set_field("db_url", "postgresql://test:test@127.0.0.1:5433/ava")
    ensure_pg_binaries_step(contexts[0])
    ensure_pg_binaries_step(contexts[1])
    assert calls == ["local", "local"]
