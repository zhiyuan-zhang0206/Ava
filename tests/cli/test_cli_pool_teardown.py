"""Regression coverage for deterministic CLI connection-pool teardown."""

from __future__ import annotations

import contextlib
from pathlib import Path
from types import SimpleNamespace

import pytest

from cli.commands._converge_extensions import (
    adopt_local_extensions,
    materialize_cluster_extensions,
)
from cli.commands._skill_package import _register_in_cluster
from shared import db, extension_adopt, extension_materialize, paths


class _PoolSpy:
    """ConnectionPool stand-in recording close(); supports ``with pool``."""

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
    """Replace the lazy CLI pool factory with a pool whose close is observable."""
    spy = _PoolSpy()
    monkeypatch.setattr(db, "pool", lambda: spy)
    return spy


def test_materialize_cluster_extensions_closes_pool_on_success(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Materialization closes its eagerly opened worker-owning pool after use."""
    spy = _install_pool_spy(monkeypatch)
    monkeypatch.setattr(paths, "skills_dir", lambda: tmp_path)

    def _noop_materialize(_conn: object, *, _dest_root: Path) -> SimpleNamespace:
        return SimpleNamespace(landed=[], updated=[], kept_local_edits=[], missing_blob=[])

    monkeypatch.setattr(extension_materialize, "materialize_skills", _noop_materialize)

    materialize_cluster_extensions()

    assert spy.closed


def test_materialize_cluster_extensions_closes_pool_when_materialize_raises(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The best-effort materialization failure path still closes the pool."""
    spy = _install_pool_spy(monkeypatch)
    monkeypatch.setattr(paths, "skills_dir", lambda: tmp_path)

    def raise_materialize(*args: object, **kwargs: object) -> None:
        raise RuntimeError("registry unavailable")

    monkeypatch.setattr(extension_materialize, "materialize_skills", raise_materialize)

    materialize_cluster_extensions()

    assert spy.closed


def test_adopt_local_extensions_closes_pool_on_success(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Adoption closes its eagerly opened worker-owning pool after use."""
    spy = _install_pool_spy(monkeypatch)
    monkeypatch.setattr(paths, "skills_dir", lambda: tmp_path)

    def _noop_adopt(_pool: object, *, _skills_root: Path) -> SimpleNamespace:
        return SimpleNamespace(adopted=[], missing_tree=[], conflicts=[])

    monkeypatch.setattr(extension_adopt, "adopt_local_installs", _noop_adopt)

    adopt_local_extensions()

    assert spy.closed


def test_adopt_local_extensions_closes_pool_when_adopt_raises(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The best-effort adoption failure path still closes the pool."""
    spy = _install_pool_spy(monkeypatch)
    monkeypatch.setattr(paths, "skills_dir", lambda: tmp_path)

    def raise_adopt(*args: object, **kwargs: object) -> None:
        raise RuntimeError("registry unavailable")

    monkeypatch.setattr(extension_adopt, "adopt_local_installs", raise_adopt)

    adopt_local_extensions()

    assert spy.closed


def test_register_in_cluster_closes_pool(monkeypatch: pytest.MonkeyPatch) -> None:
    """Registration closes its pool even when there are no packages to register."""
    spy = _install_pool_spy(monkeypatch)

    _register_in_cluster([], source="https://example.com/repo.git", ref="main")

    assert spy.closed
