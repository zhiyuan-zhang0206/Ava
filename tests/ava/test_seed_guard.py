"""The _ensure_agents_meta_row seed helper must refuse a production DB URL
before opening any connection.

2026-08-12 incident class: synthetic agent rows (spawner="test", high-range
ids) written into the production agents/agents_meta tables. The helper's guard
lives in shared/test_db_guard.py (single source of truth); these tests prove
the wiring — that the helper actually calls it — without touching a database.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from shared.config import settings


def _load_conftest() -> object:
    """Import tests/ava/conftest.py as a module (it is not a package import —
    tests/ has no __init__.py). Module-level side effects are import-only."""
    path = Path(__file__).resolve().parent / "conftest.py"
    spec = importlib.util.spec_from_file_location("ava_conftest_under_test", path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


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
