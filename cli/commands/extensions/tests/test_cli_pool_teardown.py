from __future__ import annotations

import contextlib
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from base.db.tests.fakes import patch_database
from base.packages.extensions import adopt, materialize
from cli.commands.extensions.materialize import (
    adopt_local_extensions,
    materialize_cluster_extensions,
)
from cli.commands.extensions.skill_package import register_in_cluster
from cli.database.runtime import CommandRuntime
from cli.database.tests.command_inputs import command_runtime as command_runtime
from tests.path_scoped.cli_tests import operator_database as operator_database


class _PoolSpy:
    def __init__(self) -> None:
        self.closed = False

    def __enter__(self) -> _PoolSpy:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        self.closed = True

    def connection(self) -> contextlib.AbstractContextManager[object]:
        return contextlib.nullcontext(object())


def _install_pool_spy(monkeypatch: pytest.MonkeyPatch) -> _PoolSpy:
    spy = _PoolSpy()
    patch_database(monkeypatch, pool=lambda: spy)
    return spy


def test_materialize_cluster_extensions_closes_pool_on_success(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, operator_database: Callable[[], Any]
) -> None:
    spy = _install_pool_spy(monkeypatch)

    def _noop_materialize(_conn: object, *, _dest_root: Path) -> SimpleNamespace:
        return SimpleNamespace(landed=[], updated=[], kept_local_edits=[], missing_blob=[])

    monkeypatch.setattr(materialize, "materialize_skills", _noop_materialize)

    materialize_cluster_extensions(database_factory=operator_database, skills_root=tmp_path)

    assert spy.closed


def test_materialize_cluster_extensions_closes_pool_when_materialize_raises(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, operator_database: Callable[[], Any]
) -> None:
    spy = _install_pool_spy(monkeypatch)

    def raise_materialize(*args: object, **kwargs: object) -> None:
        raise RuntimeError("registry unavailable")

    monkeypatch.setattr(materialize, "materialize_skills", raise_materialize)

    materialize_cluster_extensions(database_factory=operator_database, skills_root=tmp_path)

    assert spy.closed


def test_adopt_local_extensions_closes_pool_on_success(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    operator_database: Callable[[], Any],
    *,
    command_runtime: CommandRuntime,
) -> None:
    spy = _install_pool_spy(monkeypatch)

    def _noop_adopt(_pool: object, *, _skills_root: Path) -> SimpleNamespace:
        return SimpleNamespace(adopted=[], missing_tree=[], conflicts=[])

    monkeypatch.setattr(adopt, "adopt_local_installs", _noop_adopt)

    adopt_local_extensions(
        database_factory=operator_database,
        machine_name=command_runtime.machine.name,
        skills_root=tmp_path,
    )

    assert spy.closed


def test_adopt_local_extensions_closes_pool_when_adopt_raises(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    operator_database: Callable[[], Any],
    *,
    command_runtime: CommandRuntime,
) -> None:
    spy = _install_pool_spy(monkeypatch)

    def raise_adopt(*args: object, **kwargs: object) -> None:
        raise RuntimeError("registry unavailable")

    monkeypatch.setattr(adopt, "adopt_local_installs", raise_adopt)

    adopt_local_extensions(
        database_factory=operator_database,
        machine_name=command_runtime.machine.name,
        skills_root=tmp_path,
    )

    assert spy.closed


def test_register_in_cluster_closes_pool(
    monkeypatch: pytest.MonkeyPatch,
    operator_database: Callable[[], Any],
    *,
    command_runtime: CommandRuntime,
) -> None:
    spy = _install_pool_spy(monkeypatch)

    register_in_cluster(
        [],
        source="https://example.com/repo.git",
        ref="main",
        database_factory=operator_database,
        machine=command_runtime.machine,
    )

    assert spy.closed
