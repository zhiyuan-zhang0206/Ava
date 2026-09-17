"""Deferred-delivery outbox (task #3757) — journal, sender keys, flush loop.

The sender half records failed deliveries durably and shares one idempotency
key across a retry chain; the flusher half redelivers due records through the
canonical chat-inbound path, retires them exactly once, and abandons — loudly,
with the record kept — what its budget or a permanent failure refuses.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import psycopg
import pytest
from psycopg_pool import ConnectionPool

from shared import delivery_outbox as outbox
from shared.config import settings
from shared.db import create_agent

_NOW = datetime(2026, 9, 17, 9, 30, 0, tzinfo=UTC)


def _limits(**overrides: object) -> outbox.DeliveryOutboxLimits:
    base: dict[str, object] = {
        "enabled": True,
        "retry_backoff_steps": (30.0, 60.0, 300.0, 900.0),
        "budget_seconds": 43200.0,
        "dedup_window_seconds": 900.0,
        "flush_interval_seconds": 30.0,
        "max_entries": 128,
    }
    base.update(overrides)
    return outbox.DeliveryOutboxLimits(**base)  # type: ignore[arg-type]


@pytest.fixture()
def journal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    monkeypatch.setattr(settings.general, "ava_home", str(tmp_path))
    outbox._reset_caches_for_tests()
    yield tmp_path
    outbox._reset_caches_for_tests()


@pytest.fixture()
def pool() -> Iterator[ConnectionPool]:
    import shared.db

    p = shared.db.pool(max_size=2)
    yield p
    p.close()


def _patch_limits(monkeypatch: pytest.MonkeyPatch, **overrides: object) -> None:
    snapshot = _limits(**overrides)
    monkeypatch.setattr(outbox, "limits", lambda: snapshot)
    outbox._reset_caches_for_tests()


def _agent(db_conn: psycopg.Connection, status: str = "running") -> int:
    # `create_agent` inserts the `agents` row only; the delivery machinery reads
    # `agents_meta`, so the helper materializes it like the spawn path does.
    agent_id = create_agent(db_conn)
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO agents_meta (id, spawner, status) VALUES (%s, 'user', %s)",
            (agent_id, status),
        )
    db_conn.commit()
    return agent_id


def _record(
    *,
    agent_id: int,
    source: str = "watcher:7",
    content: str = "the daily check fired",
    key: str = "key-1",
    now: datetime | None = None,
) -> Path | None:
    return outbox.record_failed_send(
        agent_id=agent_id,
        source=source,
        content=content,
        client_message_id=key,
        now=now,
    )


def _entries() -> list[outbox.OutboxEntry]:
    directory = outbox.journal_dir()
    if not directory.is_dir():
        return []
    return [
        entry
        for path in sorted(directory.glob("*.json"))
        if (entry := outbox._read(path)) is not None
    ]


def _inbounds(db_conn: psycopg.Connection, agent_id: int) -> list[tuple[str, str, str]]:
    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT content, source, client_message_id FROM inbound_messages "
            "WHERE agent_id = %s ORDER BY id",
            (agent_id,),
        )
        return [(str(r[0]), str(r[1]), str(r[2])) for r in cur.fetchall()]


# ── sender half: recording, merging, keys ────────────────────────────────────


def test_record_merges_same_message_within_window(journal: Path) -> None:
    """Two failures of the same (target, source, content) inside the window
    are one logical message: one file, attempt count folded, key advanced."""
    first = _record(agent_id=7, key="key-1")
    second = _record(agent_id=7, key="key-2")
    assert first is not None and second == first
    assert [p.name for p in outbox.journal_dir().glob("*.json")] == [first.name]
    entry = outbox._read(first)
    assert entry is not None and entry.state == "pending"
    assert entry.attempts == 2
    assert entry.client_message_id == "key-2"
    other = _record(agent_id=7, content="a different fire")
    assert other is not None and other != first


def test_record_splits_messages_further_apart_than_window(journal: Path) -> None:
    """Identical content after the window is a new logical message."""
    first = _record(agent_id=7, now=_NOW)
    second = _record(agent_id=7, now=_NOW + timedelta(seconds=1000))
    assert first is not None and second is not None and first != second
    assert len(list(outbox.journal_dir().glob("*.json"))) == 2


def test_record_refused_when_disabled(journal: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_limits(monkeypatch, enabled=False)
    assert _record(agent_id=7) is None
    assert not list(outbox.journal_dir().glob("*.json"))


def test_record_refused_at_entry_cap(journal: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_limits(monkeypatch, max_entries=1)
    assert _record(agent_id=7, key="key-1") is not None
    assert _record(agent_id=7, content="second message", key="key-2") is None
    assert len(list(outbox.journal_dir().glob("*.json"))) == 1


def test_logical_key_reuses_until_delivery_then_rotates(
    journal: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_limits(monkeypatch)
    first = outbox.logical_key(agent_id=7, source="watcher:7", content="check")
    assert outbox.logical_key(agent_id=7, source="watcher:7", content="check") == first
    outbox.note_send_succeeded(agent_id=7, source="watcher:7", content="check", key=first)
    assert outbox.logical_key(agent_id=7, source="watcher:7", content="check") != first
    assert outbox.logical_key(agent_id=7, source="watcher:7", content="other") != first


def test_note_send_succeeded_retires_only_the_matching_record(
    journal: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_limits(monkeypatch)
    path = _record(agent_id=7, content="check", key="key-1")
    assert path is not None
    # A different attempt's key must not retire this record.
    outbox.note_send_succeeded(agent_id=7, source="watcher:7", content="check", key="key-other")
    assert path.exists()
    outbox.note_send_succeeded(agent_id=7, source="watcher:7", content="check", key="key-1")
    assert not path.exists()


def test_split_content_matches_the_route_normalization() -> None:
    assert outbox.split_content("plain") == ("plain", None)
    # Wire strings are stripped by `_MessageContent` before the row is written
    # (strip_whitespace=True); the twin must match, or a whitespace-edged
    # replay of an already-committed key looks like a different message
    # (a false key_conflict/409).
    assert outbox.split_content("  padded\n") == ("padded", None)
    blocks: list[dict[str, object]] = [
        {"type": "text", "text": "look ma"},
        {"type": "image_url", "image_url": {"url": "https://x/i.png"}},
    ]
    text, payload = outbox.split_content(blocks)
    assert text == "look ma"
    assert payload == {"content_blocks": blocks}
    assert outbox.split_content([{"type": "image_url", "image_url": {"url": "u"}}]) == (
        "[image]",
        {"content_blocks": [{"type": "image_url", "image_url": {"url": "u"}}]},
    )


# ── flusher half: delivery, exactness, termination ───────────────────────────


def test_flush_delivers_and_retires(
    journal: Path, db_conn: psycopg.Connection, pool: ConnectionPool
) -> None:
    agent_id = _agent(db_conn)
    path = _record(agent_id=agent_id, key="key-1", now=_NOW)
    assert path is not None
    report = outbox.flush(pool, now=_NOW + timedelta(seconds=31))
    assert report.delivered == 1 and report.deferred == 0
    assert not path.exists()
    rows = _inbounds(db_conn, agent_id)
    assert len(rows) == 1
    content, source, key = rows[0]
    assert content == "the daily check fired"
    assert source == "watcher:7"
    assert key == "key-1"


def test_flush_replay_after_interrupted_retire_is_exactly_once(
    journal: Path,
    db_conn: psycopg.Connection,
    pool: ConnectionPool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A crash between the commit and the record deletion (simulated by a
    failing unlink) must not produce a second inbound on replay."""
    agent_id = _agent(db_conn)
    path = _record(agent_id=agent_id, key="key-1", now=_NOW)
    assert path is not None
    real_unlink = Path.unlink

    def _fail_unlink(self: Path, missing_ok: bool = False) -> None:
        if self == path:
            raise OSError("simulated crash between commit and unlink")
        real_unlink(self, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", _fail_unlink)
    assert outbox.flush(pool, now=_NOW + timedelta(seconds=31)).delivered == 1
    assert path.exists()
    monkeypatch.setattr(Path, "unlink", real_unlink)
    report = outbox.flush(pool, now=_NOW + timedelta(seconds=62))
    assert report.delivered == 1
    assert not path.exists()
    assert len(_inbounds(db_conn, agent_id)) == 1


def test_flush_defers_until_due_then_delivers(
    journal: Path, db_conn: psycopg.Connection, pool: ConnectionPool
) -> None:
    agent_id = _agent(db_conn)
    path = _record(agent_id=agent_id, now=_NOW)
    assert path is not None
    report = outbox.flush(pool, now=_NOW + timedelta(seconds=5))
    assert report.deferred == 1 and report.delivered == 0
    assert path.exists() and _inbounds(db_conn, agent_id) == []
    assert outbox.flush(pool, now=_NOW + timedelta(seconds=31)).delivered == 1


def test_flush_failed_attempt_backs_off(
    journal: Path,
    db_conn: psycopg.Connection,
    pool: ConnectionPool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent_id = _agent(db_conn)
    path = _record(agent_id=agent_id, now=_NOW)
    assert path is not None
    attempts: list[datetime] = []

    def _boom(_pool: object, entry: outbox.OutboxEntry, _timeout: float) -> int:
        attempts.append(_NOW)
        raise RuntimeError("data plane down")

    monkeypatch.setattr(outbox, "_deliver", _boom)
    first = outbox.flush(pool, now=_NOW + timedelta(seconds=31))
    assert first.deferred == 1 and len(attempts) == 1
    entry = outbox._read(path)
    assert entry is not None and entry.flush_attempts == 1 and entry.last_flush_at is not None
    # steps[1] = 60s: 10 seconds later the next attempt is not yet due.
    assert outbox.flush(pool, now=_NOW + timedelta(seconds=41)).deferred == 1
    assert len(attempts) == 1
    assert outbox.flush(pool, now=_NOW + timedelta(seconds=92)).deferred == 1
    assert len(attempts) == 2


def test_flush_abandons_at_budget_after_the_failed_attempt(
    journal: Path,
    db_conn: psycopg.Connection,
    pool: ConnectionPool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Past the budget the entry still gets its attempt; it is the FAILED
    attempt that abandons — with the attempt recorded on the record."""
    agent_id = _agent(db_conn)
    path = _record(agent_id=agent_id, now=_NOW)
    assert path is not None

    def _boom(_pool: object, entry: outbox.OutboxEntry, _timeout: float) -> int:
        raise RuntimeError("data plane down")

    monkeypatch.setattr(outbox, "_deliver", _boom)
    report = outbox.flush(pool, now=_NOW + timedelta(seconds=43201))
    assert report.abandoned == 1 and report.delivered == 0
    entry = outbox._read(path)
    assert entry is not None
    assert entry.state == "abandoned" and entry.abandon_reason == "budget"
    assert entry.flush_attempts == 1
    assert _inbounds(db_conn, agent_id) == []
    # An abandoned record is terminal — later passes leave it alone.
    assert outbox.flush(pool, now=_NOW + timedelta(seconds=50000)).touched == 0


def test_flush_delivers_stale_entry_when_the_final_attempt_succeeds(
    journal: Path, db_conn: psycopg.Connection, pool: ConnectionPool
) -> None:
    """A flusher stalled across the budget still gives the message its chance
    once services return: the attempt runs before the budget decision, and a
    success at any age delivers."""
    agent_id = _agent(db_conn)
    _record(agent_id=agent_id, now=_NOW)
    report = outbox.flush(pool, now=_NOW + timedelta(seconds=43201))
    assert report.delivered == 1 and report.abandoned == 0
    assert len(_inbounds(db_conn, agent_id)) == 1


def test_flush_past_budget_not_due_defers_until_the_attempt(
    journal: Path,
    db_conn: psycopg.Connection,
    pool: ConnectionPool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Past the budget, an entry not yet due waits for its next due attempt
    instead of being abandoned — the decision needs the attempt's outcome."""
    agent_id = _agent(db_conn)
    path = _record(agent_id=agent_id, now=_NOW)
    assert path is not None
    _patch_limits(monkeypatch, budget_seconds=100.0)

    def _boom(_pool: object, entry: outbox.OutboxEntry, _timeout: float) -> int:
        raise RuntimeError("data plane down")

    monkeypatch.setattr(outbox, "_deliver", _boom)
    # Attempt 1 at +31 (next due +91), attempt 2 at +95 (next due +395).
    assert outbox.flush(pool, now=_NOW + timedelta(seconds=31)).deferred == 1
    assert outbox.flush(pool, now=_NOW + timedelta(seconds=95)).deferred == 1
    # +150 is past the 100 s budget but not due: deferred, still pending.
    assert outbox.flush(pool, now=_NOW + timedelta(seconds=150)).deferred == 1
    entry = outbox._read(path)
    assert entry is not None and entry.state == "pending" and entry.flush_attempts == 2
    # The attempt owed at +395 runs and, failing past the budget, abandons.
    assert outbox.flush(pool, now=_NOW + timedelta(seconds=396)).abandoned == 1
    entry = outbox._read(path)
    assert entry is not None
    assert entry.abandon_reason == "budget" and entry.flush_attempts == 3


def test_flush_abandons_missing_agent(
    journal: Path, db_conn: psycopg.Connection, pool: ConnectionPool
) -> None:
    path = _record(agent_id=10_000_000, now=_NOW)
    assert path is not None
    report = outbox.flush(pool, now=_NOW + timedelta(seconds=31))
    assert report.abandoned == 1
    entry = outbox._read(path)
    assert entry is not None and entry.abandon_reason == "agent_missing"


def test_flush_delivers_to_terminated_owner(
    journal: Path, db_conn: psycopg.Connection, pool: ConnectionPool
) -> None:
    """A late chat must land for a terminated owner too — the delivery
    watchdog's resurrect retry is the piece that wakes it, so unlike a closure
    notice this path must not drop the row."""
    agent_id = _agent(db_conn, status="terminated")
    _record(agent_id=agent_id, now=_NOW)
    assert outbox.flush(pool, now=_NOW + timedelta(seconds=31)).delivered == 1
    assert len(_inbounds(db_conn, agent_id)) == 1


def test_flush_disabled_touches_nothing(
    journal: Path,
    db_conn: psycopg.Connection,
    pool: ConnectionPool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent_id = _agent(db_conn)
    path = _record(agent_id=agent_id, now=_NOW)
    assert path is not None
    _patch_limits(monkeypatch, enabled=False)
    report = outbox.flush(pool, now=_NOW + timedelta(seconds=31))
    assert report.deferred == 1
    assert path.exists() and _inbounds(db_conn, agent_id) == []


def test_flush_keeps_unreadable_record(journal: Path, pool: ConnectionPool) -> None:
    outbox.journal_dir().mkdir(parents=True, exist_ok=True)
    bad = outbox.journal_dir() / "7_deadbeefdeadbeef_1.json"
    bad.write_text("{not json", encoding="utf-8")
    report = outbox.flush(pool)
    assert report.unreadable == 1
    assert bad.exists()
