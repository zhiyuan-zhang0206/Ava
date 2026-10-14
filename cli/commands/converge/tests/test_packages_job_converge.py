"""Converge registers the recurring packages-refresh OS job."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from base.host.system import packages_job as job
from base.host.tests.fake_commands import fake_host
from base.sessions.tests.refuse_helper import refuse_helper
from base.telemetry import EventPipeline
from cli.commands.converge.spec import ConvergeCtx
from cli.database.runtime import CommandRuntime
from cli.database.tests.command_inputs import command_runtime as command_runtime
from tests.path_scoped.cli_tests import operator_database as operator_database
from tests.path_scoped.cli_tests import operator_pipeline as operator_pipeline


def test_converge_registers_the_refresh_job(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    operator_database: Callable[[], Any],
    operator_pipeline: Callable[[], EventPipeline],
    *,
    command_runtime: CommandRuntime,
) -> None:
    from cli.commands.converge.os_jobs import ensure_packages_refresh_job

    calls: list[str] = []

    def register(
        *, enabled_reader: object, refresh_enabled_reader: object, tick_reader: object
    ) -> None:
        calls.append("register")

    monkeypatch.setattr(job, "register_packages_job", register)

    ctx = ConvergeCtx(
        repo=tmp_path,
        ava_home=tmp_path,
        roles=None,
        config=command_runtime.config,
        database_factory=operator_database,
        producer=operator_pipeline,
        machine=command_runtime.machine,
        helper=refuse_helper,
        host=fake_host(),
        services=frozenset(),
    )
    ensure_packages_refresh_job(ctx)
    assert calls == ["register"]
