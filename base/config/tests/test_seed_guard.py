"""The _ensure_agents_meta_row seed helper must refuse a production DB URL
before opening any connection.

2026-08-12 incident class: synthetic agent rows (spawner="test", high-range
ids) written into the production agents/agents_meta tables. The helper's guard
lives in base/db/test_db_guard.py (single source of truth); these tests prove
the wiring — that the helper actually calls it — without touching a database.
"""

from __future__ import annotations

import importlib

import pytest

from base.config import settings


def _load_conftest() -> object:
    """The module that holds the `tests/ava` fixtures and the seed helper."""
    return importlib.import_module("tests.path_scoped.ava_tests")


def test_ensure_agents_meta_row_refuses_prod_db(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conftest = _load_conftest()
    monkeypatch.setattr(
        settings.data_plane,
        "db_url",
        "postgresql://ava_main:***@10.0.0.2:6433/ava_main",
    )
    with pytest.raises(RuntimeError, match="production database"):
        conftest._ensure_agents_meta_row(900_000)  # type: ignore[attr-defined]


def test_ensure_agents_meta_row_refuses_before_connecting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The guard fires before psycopg.connect is ever called — a prod URL can
    never reach the wire."""
    conftest = _load_conftest()
    monkeypatch.setattr(
        settings.data_plane, "db_url", "postgresql://ava_main@127.0.0.1:6433/ava_main"
    )

    def _boom(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("_ensure_agents_meta_row connected despite the guard")

    monkeypatch.setattr("psycopg.connect", _boom)
    with pytest.raises(RuntimeError, match="production database"):
        conftest._ensure_agents_meta_row(900_000)  # type: ignore[attr-defined]
