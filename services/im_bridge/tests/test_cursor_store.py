"""`services.im_bridge.cursor_store` — the push watermark's two keys round-trip.

The watermark gained `created_at` as its primary key (task #4933): a compact
renumbers a session's item ids, so an id-only position can strand above the
whole live numbering. The SQL runs against an in-memory pool double here —
these assertions are about which columns the statements carry and how a
stamped row differs from a legacy one; the pool itself is psycopg''s.
"""

from __future__ import annotations

from typing import Any

from services.im_bridge.cursor_store import CursorStore, PushWatermark


class _FakeDB:
    def __init__(self, rows: list[tuple[Any, ...]]) -> None:
        self.rows = rows
        self.statements: list[tuple[str, tuple[Any, ...] | None]] = []


class _FakeCursor:
    def __init__(self, db: _FakeDB) -> None:
        self._db = db

    def __enter__(self) -> _FakeCursor:
        return self

    def __exit__(self, *exc: object) -> bool:
        return False

    def execute(self, statement: str, params: tuple[Any, ...] | None = None) -> None:
        self._db.statements.append((statement, params))

    def fetchall(self) -> list[tuple[Any, ...]]:
        return self._db.rows


class _FakeConn:
    def __init__(self, db: _FakeDB) -> None:
        self._db = db

    def __enter__(self) -> _FakeConn:
        return self

    def __exit__(self, *exc: object) -> bool:
        return False

    def execute(self, statement: str, params: tuple[Any, ...] | None = None) -> None:
        self._db.statements.append((statement, params))

    def cursor(self) -> _FakeCursor:
        return _FakeCursor(self._db)


class _FakePool:
    """The pool surface `CursorStore` touches: `connection()` as a context manager."""

    def __init__(self, rows: list[tuple[Any, ...]] | None = None) -> None:
        self.db = _FakeDB(rows or [])

    def connection(self, timeout: float | None = None) -> _FakeConn:
        return _FakeConn(self.db)


def test_load_push_carries_created_at_and_none_for_legacy() -> None:
    """A stamped row loads as (created_at, item_id); a row written before the
    column existed loads with a None stamp and compares by item_id alone."""
    pool = _FakePool(
        rows=[
            ("telegram", "12345", 405, "128.1", "2026-10-03T16:21:00+00:00"),
            ("feishu", "oc_1", 1818, "377.1", None),
        ]
    )
    loaded = CursorStore(pool).load_push()
    assert loaded == {
        ("telegram", "12345", 405): PushWatermark("2026-10-03T16:21:00+00:00", "128.1"),
        ("feishu", "oc_1", 1818): PushWatermark(None, "377.1"),
    }
    statement, params = pool.db.statements[0]
    assert "push_created_at" in statement
    assert params is None


def test_save_push_writes_both_keys() -> None:
    """The upsert carries the stamp next to the item id; a legacy watermark
    writes a NULL stamp rather than fabricating one."""
    pool = _FakePool()
    store = CursorStore(pool)
    store.save_push("telegram", "12345", 405, PushWatermark("2026-10-03T16:30:00+00:00", "129.0"))
    store.save_push("feishu", "oc_1", 1818, PushWatermark(None, "377.1"))
    inserts = [
        (statement, params)
        for statement, params in pool.db.statements
        if "INSERT INTO im_bridge_cursors" in statement
    ]
    statement, params = inserts[0]
    assert "push_created_at = EXCLUDED.push_created_at" in statement
    assert params == ("telegram", "12345", 405, "129.0", "2026-10-03T16:30:00+00:00")
    assert inserts[1][1] == ("feishu", "oc_1", 1818, "377.1", None)
