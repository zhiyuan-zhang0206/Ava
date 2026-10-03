"""Test support: a database that accepts every shell-session statement."""

from __future__ import annotations


class FakeDatabase:
    """Every statement succeeds; the `session_index` update returns `next_index + 1`."""

    def __init__(self, next_index: int = 1) -> None:
        self._row = (next_index + 1,)

    def connect(self) -> FakeDatabase:
        return self

    def __enter__(self) -> FakeDatabase:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def cursor(self) -> FakeDatabase:
        return self

    def execute(self, *_args: object) -> None:
        return None

    def fetchone(self) -> tuple[int]:
        return self._row

    def commit(self) -> None:
        return None
