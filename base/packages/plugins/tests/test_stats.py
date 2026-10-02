"""`base.packages.plugins.stats` — the value half of declared statistics-panel cards.

Exercised against the session's real Postgres: the contract IS the upsert —
last write wins on `(plugin, id)`, the status vocabulary is closed, and what a
plugin's refresh wrote is exactly what the dashboard reads back for the
console to join against the declaration.
"""

from collections.abc import Iterator
from datetime import UTC, datetime

import psycopg
import pytest
from psycopg_pool import ConnectionPool

from base.config import settings
from base.db import pool
from base.packages.plugins import stats


@pytest.fixture
def stat_pool() -> Iterator[ConnectionPool]:
    """A sanctioned read pool on the session's test DB (caller-owned)."""
    p = pool()
    try:
        yield p
    finally:
        p.close()


def test_upsert_and_read_round_trip(stat_pool: ConnectionPool) -> None:
    stats.upsert(
        plugin="codex_usage",
        id="codex-zhang0206",
        value="6%",
        detail="94% used · weekly",
        status="warn",
        updated_by="macmini",
    )
    rows = stats.read_all(stat_pool)
    assert len(rows) == 1
    row = rows[0]
    assert (row.plugin, row.id, row.value, row.detail, row.status) == (
        "codex_usage",
        "codex-zhang0206",
        "6%",
        "94% used · weekly",
        "warn",
    )
    assert row.updated_by == "macmini"
    assert row.updated_at.tzinfo is not None
    assert abs((datetime.now(UTC) - row.updated_at).total_seconds()) < 60


def test_last_write_wins_on_the_card_key(stat_pool: ConnectionPool) -> None:
    """One row per card: the second write replaces value/detail/status in place."""
    stats.upsert(plugin="p", id="c", value="1%", status="ok")
    stats.upsert(plugin="p", id="c", value="2%", detail="retry", status="error")
    rows = stats.read_all(stat_pool)
    assert [(r.plugin, r.id, r.value, r.detail, r.status) for r in rows] == [
        ("p", "c", "2%", "retry", "error")
    ]


def test_read_all_orders_by_plugin_then_id(stat_pool: ConnectionPool) -> None:
    stats.upsert(plugin="b", id="z", value="1")
    stats.upsert(plugin="a", id="y", value="2")
    stats.upsert(plugin="a", id="x", value="3")
    assert [(r.plugin, r.id) for r in stats.read_all(stat_pool)] == [
        ("a", "x"),
        ("a", "y"),
        ("b", "z"),
    ]


def test_null_detail_stays_null(stat_pool: ConnectionPool) -> None:
    stats.upsert(plugin="p", id="c", value="42")
    assert stats.read_all(stat_pool)[0].detail is None


def test_writer_validation_refuses_what_the_panel_cannot_render(
    db_conn: psycopg.Connection,
) -> None:
    with pytest.raises(ValueError, match="non-empty string"):
        stats.upsert(plugin="p", id="c", value="  ")
    with pytest.raises(ValueError, match="exceeds"):
        stats.upsert(plugin="p", id="c", value="x" * (stats.MAX_VALUE_CHARS + 1))
    with pytest.raises(ValueError, match="exceeds"):
        stats.upsert(
            plugin="p",
            id="c",
            value="ok",
            detail="y" * (settings.display.plugin_stats_max_detail_chars + 1),
        )
    with pytest.raises(ValueError, match="not one of"):
        stats.upsert(plugin="p", id="c", value="ok", status="empty")
    with pytest.raises(ValueError, match="non-empty string"):
        stats.upsert(plugin="", id="c", value="ok")


def test_detail_cap_follows_config(
    monkeypatch: pytest.MonkeyPatch, db_conn: psycopg.Connection
) -> None:
    """The detail cap is cluster config, resolved per call: a shortened cap
    rejects what the default would accept (task #3696)."""
    monkeypatch.setattr(settings.display, "plugin_stats_max_detail_chars", 10)
    stats.upsert(plugin="p", id="c", value="ok", detail="y" * 10)
    with pytest.raises(ValueError, match="exceeds"):
        stats.upsert(plugin="p", id="c", value="ok", detail="y" * 11)
