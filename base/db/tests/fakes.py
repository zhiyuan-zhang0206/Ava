"""A `Database` stand-in for tests whose code under test dials through a handle but never needs
Postgres (or needs a specific connection)."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, cast

from base.db import Database


class _FakeDatabase:
    def __init__(self, connect: Callable[..., Any]) -> None:
        self.connect = connect


def fake_database(connect: Callable[..., Any]) -> Database:
    """A handle whose `connect(...)` is the given callable (any other member is absent)."""
    return cast(Database, _FakeDatabase(connect))
