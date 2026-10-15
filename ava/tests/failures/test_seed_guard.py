from __future__ import annotations

import pytest

from ava.tests.fixtures import ava_tests as conftest
from base.config import settings


def test_ensure_agents_meta_row_refuses_prod_db(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        settings.data_plane,
        "db_url",
        "postgresql://ava_main:***@10.0.0.2:6433/ava_main",
    )
    with pytest.raises(RuntimeError, match="production database"):
        conftest.ensure_agents_meta_row(900_000)


def test_ensure_agents_meta_row_refuses_before_connecting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        settings.data_plane, "db_url", "postgresql://ava_main@127.0.0.1:6433/ava_main"
    )

    def _boom(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("ensure_agents_meta_row connected despite the guard")

    monkeypatch.setattr("psycopg.connect", _boom)
    with pytest.raises(RuntimeError, match="production database"):
        conftest.ensure_agents_meta_row(900_000)
