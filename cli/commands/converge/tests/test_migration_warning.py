"""Converge reports untracked migrations without applying them."""

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from base.telemetry import EventPipeline
from cli.commands.converge import host as converge_host
from cli.commands.converge.tests.context_inputs import converge_context as _ctx
from cli.tests.bootstrap.command_inputs import command_runtime as command_runtime
from tests.path_scoped.cli_tests import operator_database as operator_database
from tests.path_scoped.cli_tests import operator_pipeline as operator_pipeline


class TestWarnUntrackedMigrations:
    """The converge step that surfaces untracked migrations/ files to the operator."""

    def test_warns_and_lists_untracked_files(
        self,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        operator_database: Callable[[], Any],
        operator_pipeline: Callable[[], EventPipeline],
    ) -> None:
        monkeypatch.setattr(
            "base.deploy.schema.migrations.untracked_migration_files",
            lambda: ["20260808T010000_add-foo.sql"],
        )
        converge_host._warn_untracked_migrations(
            _ctx(
                tmp_path, tmp_path, operator_database=operator_database, producer=operator_pipeline
            )
        )
        out = capsys.readouterr().out
        assert "untracked" in out
        assert "20260808T010000_add-foo.sql" in out
        assert "NOT" in out and "will NOT be applied" in out

    def test_silent_when_nothing_untracked(
        self,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        operator_database: Callable[[], Any],
        operator_pipeline: Callable[[], EventPipeline],
    ) -> None:
        monkeypatch.setattr("base.deploy.schema.migrations.untracked_migration_files", list)
        converge_host._warn_untracked_migrations(
            _ctx(
                tmp_path, tmp_path, operator_database=operator_database, producer=operator_pipeline
            )
        )
        assert capsys.readouterr().out == ""

    def test_registered_gateway_only(self) -> None:
        """The warning is wired into CONVERGE_STEPS with gateway-only roles: the
        gateway is the single schema writer, so only its console should carry it."""
        step = next(
            s for s in converge_host.CONVERGE_STEPS if s.name == "untracked migrations warning"
        )
        assert step.roles == frozenset({"gateway"})
