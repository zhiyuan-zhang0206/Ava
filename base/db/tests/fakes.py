"""A `Database` stand-in for tests whose code under test dials through a handle but never needs
Postgres (or needs a specific connection)."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, cast

import pytest

from base.db import Database


class _FakeDatabase:
    def __init__(self, connect: Callable[..., Any]) -> None:
        self.connect = connect


def fake_database(connect: Callable[..., Any]) -> Database:
    """A handle whose `connect(...)` is the given callable (any other member is absent)."""
    return cast(Database, _FakeDatabase(connect))


def patch_database(
    monkeypatch: pytest.MonkeyPatch,
    *,
    connect: Callable[..., Any] | None = None,
    pool: Callable[..., Any] | None = None,
    direct_url: Callable[..., Any] | None = None,
) -> None:
    """Replace the dial methods of every `Database` with the given callables, which receive the
    call's own arguments (not the handle): for tests of a root that builds its handle with
    `Database.from_settings()` and so offers no instance to substitute."""
    for method, replacement in (("connect", connect), ("pool", pool), ("direct_url", direct_url)):
        if replacement is not None:
            monkeypatch.setattr(Database, method, _unbound(replacement))


def _unbound(replacement: Callable[..., Any]) -> Callable[..., Any]:
    def method(_self: Database, *args: Any, **kwargs: Any) -> Any:
        return replacement(*args, **kwargs)

    return method
