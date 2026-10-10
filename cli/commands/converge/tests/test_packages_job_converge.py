"""Converge registers the recurring packages-refresh OS job."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from base.config import ConfigBoot
from base.host.system import packages_job as job
from base.telemetry import EventPipeline
from cli.commands.converge.spec import ConvergeCtx
from tests.path_scoped.cli_tests import operator_database as operator_database
from tests.path_scoped.cli_tests import operator_pipeline as operator_pipeline


def test_converge_registers_the_refresh_job(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    operator_database: Callable[[], Any],
    operator_pipeline: Callable[[], EventPipeline],
) -> None:
    from cli.commands.converge._os_jobs import ensure_packages_refresh_job

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
        config=ConfigBoot(),
        database_factory=operator_database,
        producer=operator_pipeline,
    )
    ensure_packages_refresh_job(ctx)
    assert calls == ["register"]
