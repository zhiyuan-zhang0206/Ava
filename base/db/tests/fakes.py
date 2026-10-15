from __future__ import annotations

from collections.abc import Callable
from typing import Any, cast

import pytest

from base.db import Database


class _FakeDatabase:
    def __init__(self, connect: Callable[..., Any]) -> None:
        self.connect = connect


def fake_database(connect: Callable[..., Any]) -> Database:
    return cast(Database, _FakeDatabase(connect))


def patch_database(
    monkeypatch: pytest.MonkeyPatch,
    *,
    connect: Callable[..., Any] | None = None,
    pool: Callable[..., Any] | None = None,
    direct_url: Callable[..., Any] | None = None,
) -> None:
    for method, replacement in (("connect", connect), ("pool", pool), ("direct_url", direct_url)):
        if replacement is not None:
            monkeypatch.setattr(Database, method, _unbound(replacement))


def _unbound(replacement: Callable[..., Any]) -> Callable[..., Any]:
    def method(_self: Database, *args: Any, **kwargs: Any) -> Any:
        return replacement(*args, **kwargs)

    return method
