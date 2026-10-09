"""`Database`: a handle dials its own `DbConfig`, and the module-level entry points are the
same dial built from the live settings at each call."""

from __future__ import annotations

import dataclasses
import threading
from typing import Any

import pytest

from base.config import get_field, settings
from base.db import Database, DbConfig, connect, connections
from base.db.config import db_config_from_settings

_URL = "postgresql://handle_user:pw@db.example:5432/handle_db"


def _config(**overrides: Any) -> DbConfig:
    base = DbConfig(
        db_url=_URL,
        db_sslmode="",
        db_pool_min_size=1,
        db_pool_max_size=2,
        pgbouncer_enabled=False,
    )
    return dataclasses.replace(base, **overrides)


class _Dialed:
    """Records the dials made by the test's own thread.

    The monkeypatch sits on the process-wide ``psycopg.connect``, so a dial from
    another thread sharing the xdist worker can reach the recorder; recording it
    would break the assertions that index the list (2026-10-03 shard8: a
    concurrent dial preceded the handle's own, ``kwargs[0]`` KeyError).
    """

    def __init__(self) -> None:
        self.urls: list[str] = []
        self.kwargs: list[dict[str, Any]] = []
        self._owner = threading.current_thread()

    def connect(self, url: str, **kwargs: Any) -> object:
        if threading.current_thread() is self._owner:
            self.urls.append(url)
            self.kwargs.append(kwargs)
        return _FakeConn()


class _FakeConn:
    def execute(self, *_args: object) -> object:
        return self

    def __enter__(self) -> _FakeConn:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None


@pytest.fixture
def dialed(monkeypatch: pytest.MonkeyPatch) -> _Dialed:
    rec = _Dialed()
    monkeypatch.setattr(connections.psycopg, "connect", rec.connect)

    def no_restore(_conn: object) -> None:
        return None

    monkeypatch.setattr(connections, "_restore_pooled_session", no_restore)
    return rec


def test_a_handle_dials_its_own_config_not_the_settings(dialed: _Dialed) -> None:
    Database(_config()).connect()
    assert dialed.urls == [_URL]
    assert settings.data_plane.db_url != _URL


def test_a_handle_applies_its_sslmode_when_the_url_is_silent(dialed: _Dialed) -> None:
    Database(_config(db_sslmode="require")).connect()
    assert dialed.kwargs[0]["sslmode"] == "require"


def test_the_module_level_connect_builds_the_live_settings_each_call(
    dialed: _Dialed, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings.data_plane, "db_url", "postgresql://u:p@first.example:1/x")
    connect()
    monkeypatch.setattr(settings.data_plane, "db_url", "postgresql://u:p@second.example:2/y")
    connect()
    assert dialed.urls == [
        "postgresql://u:p@first.example:1/x",
        "postgresql://u:p@second.example:2/y",
    ]


def test_the_direct_url_of_a_handle_without_a_pooler_is_its_url() -> None:
    assert Database(_config()).direct_url() == _URL


def test_a_write_transaction_declares_itself_writable(dialed: _Dialed) -> None:
    with Database(_config()).write_transaction() as conn:
        assert isinstance(conn, _FakeConn)
    assert dialed.urls == [_URL]


def test_the_config_carries_the_live_value_of_every_field(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings.data_plane, "db_pool_max_size", 7)
    config = db_config_from_settings()
    for field in dataclasses.fields(config):
        if field.name != "db_authority_refusal":
            assert getattr(config, field.name) == get_field(field.name), field.name
    assert config.db_authority_refusal == settings.env_boot.db_authority_refusal
    assert config.db_pool_max_size == 7
    assert "handle_user" not in repr(_config())


@pytest.mark.parametrize("operation", ["connect", "direct", "pool", "direct_pool", "async_pool"])
def test_all_handle_dials_use_their_retained_authority_refusal(operation: str) -> None:
    """Every configured connection entry point refuses before opening a client."""
    from psycopg_pool import AsyncConnectionPool

    handle = Database(
        _config(db_url="postgresql://ava@127.0.0.1:1/x", db_authority_refusal="unadmitted runtime")
    )
    with pytest.raises(connections.NoDatabaseAuthorityError, match="unadmitted runtime"):
        if operation == "connect":
            handle.connect()
        elif operation == "direct":
            handle.connect(direct=True)
        elif operation == "pool":
            handle.pool()
        elif operation == "direct_pool":
            handle.pool(direct=True)
        else:
            handle.async_pool(AsyncConnectionPool, min_size=1, max_size=2, timeout=1)
