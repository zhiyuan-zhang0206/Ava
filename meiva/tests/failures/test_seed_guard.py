from __future__ import annotations

import pytest

from base.config import settings
from meiva.tests.fixtures import meiva_tests as conftest


def test_ensure_agents_meta_row_refuses_prod_db(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        settings.data_plane,
        "db_url",
        "postgresql://meiva_main:***@10.0.0.2:6433/meiva_main",
    )
    with pytest.raises(RuntimeError, match="production database"):
        conftest.ensure_agents_meta_row(900_000)


def test_ensure_agents_meta_row_refuses_before_connecting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        settings.data_plane, "db_url", "postgresql://meiva_main@127.0.0.1:6433/meiva_main"
    )

    def _boom(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("ensure_agents_meta_row connected despite the guard")

    monkeypatch.setattr("psycopg.connect", _boom)
    with pytest.raises(RuntimeError, match="production database"):
        conftest.ensure_agents_meta_row(900_000)
