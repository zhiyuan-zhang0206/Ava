"""Deferred-delivery outbox (task #3757) — journal, sender keys, flush loop.

The sender half records failed deliveries durably and shares one idempotency
key across a retry chain; the flusher half redelivers due records through the
canonical chat-inbound path, retires them exactly once, and abandons — loudly,
with the record kept — what its budget or a permanent failure refuses.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import Mock

import psycopg
import pytest
from psycopg_pool import ConnectionPool

from shared import delivery_outbox as outbox
from shared.chat_delivery import insert_chat_inbound_once
from shared.config import settings
from shared.db import create_agent

_NOW = datetime(2026, 9, 17, 9, 30, 0, tzinfo=UTC)


def _limits(**overrides: object) -> outbox.DeliveryOutboxLimits:
    base: dict[str, object] = {
        "enabled": True,
        "retry_backoff_steps": (30.0, 60.0, 300.0, 900.0),
        "budget_seconds": 43200.0,
        "abandoned_retention_days": 30,
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


def test_read_accepts_legacy_fingerprint_without_completion_metadata(journal: Path) -> None:
    """Old pending entries remain replayable when their metadata is absent."""
    content = "the daily check fired"
    legacy_raw = f"7\x1fwatcher:7\x1f{outbox._canonical_content(content)}"
    legacy_fingerprint = hashlib.sha256(legacy_raw.encode("utf-8")).hexdigest()[:16]
    entry = outbox.OutboxEntry(
        schema_version=1,
        agent_id=7,
        source="watcher:7",
        content=content,
        client_message_id="legacy-key",
        created_at=_NOW.isoformat(),
        last_attempt_at=_NOW.isoformat(),
        attempts=1,
        origin_agent_id=None,
        origin_pid=None,
        flush_attempts=0,
        last_flush_at=None,
        state="pending",
        abandon_reason=None,
        abandon_detail=None,
        abandoned_at=None,
    )
    path = outbox.journal_dir() / outbox._entry_path_name(7, legacy_fingerprint, _NOW)
    outbox._write_atomic(path, entry)

    assert outbox._read(path) == entry


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


def test_flush_replays_hourly_completion_through_the_policy_boundary(
    journal: Path,
    db_conn: psycopg.Connection,
    pool: ConnectionPool,
) -> None:
    """A gateway outage cannot bypass hourly suppression on the outbox replay."""
    agent_id = _agent(db_conn)
    with db_conn.cursor() as cur:
        cur.execute(
            "UPDATE agents_meta SET config_overlay = %s::jsonb WHERE id = %s",
            ('{"completion_notice_policy": "hourly"}', agent_id),
        )
    db_conn.commit()
    path = outbox.record_failed_send(
        agent_id=agent_id,
        source="shell:77",
        content="Background command 'build' exited with code 0. Full output at build.log.",
        client_message_id="hourly-key",
        completion_notice={"outcome": "exit", "exit_code": 0},
        now=_NOW,
    )
    assert path is not None

    report = outbox.flush(pool, now=_NOW + timedelta(seconds=31))

    assert report.delivered == 0 and report.buffered == 1
    assert not path.exists()
    assert _inbounds(db_conn, agent_id) == []
    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT source, exit_code FROM completion_notice_events WHERE agent_id = %s",
            (agent_id,),
        )
        assert cur.fetchone() == ("shell:77", 0)


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
    assert entry.abandon_detail == "data plane down"
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
    assert entry.abandon_detail == "data plane down"


def test_flush_expires_abandoned_records_after_retention(
    journal: Path, db_conn: psycopg.Connection, pool: ConnectionPool
) -> None:
    """The inspection window is bounded: an abandoned record past its
    retention is pruned by the next pass, without being re-attempted."""
    agent_id = _agent(db_conn)
    path = _record(agent_id=agent_id, now=_NOW)
    assert path is not None
    entry = outbox._read(path)
    assert entry is not None
    outbox._abandon(path, entry, "budget", _NOW)
    report = outbox.flush(pool, now=_NOW + timedelta(days=31))
    assert report.expired == 1 and not path.exists()
    assert outbox.flush(pool, now=_NOW + timedelta(days=32)).expired == 0


def test_flush_keeps_abandoned_records_inside_retention(
    journal: Path, db_conn: psycopg.Connection, pool: ConnectionPool
) -> None:
    agent_id = _agent(db_conn)
    path = _record(agent_id=agent_id, now=_NOW)
    assert path is not None
    entry = outbox._read(path)
    assert entry is not None
    outbox._abandon(path, entry, "budget", _NOW)
    assert outbox.flush(pool, now=_NOW + timedelta(days=29)).expired == 0
    assert path.exists()


def test_flush_disabled_keeps_expired_records(
    journal: Path,
    db_conn: psycopg.Connection,
    pool: ConnectionPool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The kill switch is inert, never destructive: no expiry while off."""
    agent_id = _agent(db_conn)
    path = _record(agent_id=agent_id, now=_NOW)
    assert path is not None
    entry = outbox._read(path)
    assert entry is not None
    outbox._abandon(path, entry, "budget", _NOW)
    _patch_limits(monkeypatch, enabled=False)
    report = outbox.flush(pool, now=_NOW + timedelta(days=31))
    assert report.expired == 0 and path.exists()


def test_flush_retention_boundary_is_inclusive(
    journal: Path, db_conn: psycopg.Connection, pool: ConnectionPool
) -> None:
    """The window closes exactly at the threshold (>=): one second short the
    records stay, the exact 30 days expire them."""
    agent_id = _agent(db_conn)
    first = _record(agent_id=agent_id, content="first", key="k-1", now=_NOW)
    second = _record(agent_id=agent_id, content="second", key="k-2", now=_NOW)
    assert first is not None and second is not None
    for path in (first, second):
        entry = outbox._read(path)
        assert entry is not None
        outbox._abandon(path, entry, "budget", _NOW)
    report = outbox.flush(pool, now=_NOW + timedelta(days=30) - timedelta(seconds=1))
    assert report.expired == 0 and first.exists() and second.exists()
    report = outbox.flush(pool, now=_NOW + timedelta(days=30))
    assert report.expired == 2 and not first.exists() and not second.exists()


def test_flush_abandons_missing_agent(
    journal: Path, db_conn: psycopg.Connection, pool: ConnectionPool
) -> None:
    path = _record(agent_id=10_000_000, now=_NOW)
    assert path is not None
    report = outbox.flush(pool, now=_NOW + timedelta(seconds=31))
    assert report.abandoned == 1
    entry = outbox._read(path)
    assert entry is not None and entry.abandon_reason == "agent_missing"
    # Detected by the flusher itself — no upstream message exists; detail stays None.
    assert entry.abandon_detail is None


def test_flush_abandons_caller_protocol_carrying_the_refusal_detail(
    journal: Path,
    db_conn: psycopg.Connection,
    pool: ConnectionPool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A gate refusal keeps its stable code AND carries the readable refusal
    text (task #4095): the record explains itself without a log dig."""
    emit = Mock()
    monkeypatch.setattr("shared.telemetry.emit", emit)
    agent_id = _agent(db_conn)
    path = _record(agent_id=agent_id, source="external_agent:codex:run-42", now=_NOW)
    assert path is not None
    report = outbox.flush(pool, now=_NOW + timedelta(seconds=31))
    assert report.abandoned == 1
    entry = outbox._read(path)
    assert entry is not None
    assert entry.abandon_reason == "caller_protocol"
    assert entry.abandon_detail is not None
    assert entry.abandon_detail.startswith("target runtime protocol")
    abandoned = [c for c in emit.call_args_list if c.args[1] == "delivery_outbox_abandoned"]
    assert len(abandoned) == 1
    attributes = abandoned[0].kwargs["attributes"]
    assert attributes["reason"] == "caller_protocol"
    assert attributes["detail"] == entry.abandon_detail


def test_flush_abandons_key_conflict_carrying_the_conflict_detail(
    journal: Path, db_conn: psycopg.Connection, pool: ConnectionPool
) -> None:
    """The conflict message (which field diverged) reaches the record too."""
    agent_id = _agent(db_conn)
    insert_chat_inbound_once(
        db_conn,
        agent_id=agent_id,
        content="a different message",
        source="user",
        payload=None,
        client_message_id="key-1",
    )
    path = _record(agent_id=agent_id, key="key-1", now=_NOW)
    assert path is not None
    report = outbox.flush(pool, now=_NOW + timedelta(seconds=31))
    assert report.abandoned == 1
    entry = outbox._read(path)
    assert entry is not None
    assert entry.abandon_reason == "key_conflict"
    assert entry.abandon_detail is not None
    assert "already identifies a different message" in entry.abandon_detail


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


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("created_at", "garbage"),
        ("created_at", "2026-09-17T09:30:00"),  # naive — no tzinfo
        ("abandoned_at", "garbage"),
    ],
)
def test_read_treats_corrupt_timestamps_as_unreadable(
    journal: Path, field: str, value: str
) -> None:
    """A corrupt or naive timestamp makes the whole record unreadable at the
    parse boundary — never a raise inside a pass (task #3797)."""
    path = _record(agent_id=7, now=_NOW)
    assert path is not None
    raw = json.loads(path.read_text())
    raw[field] = value
    path.write_text(json.dumps(raw))
    assert outbox._read(path) is None


def test_flush_continues_past_a_corrupt_timestamp_record(
    journal: Path, db_conn: psycopg.Connection, pool: ConnectionPool
) -> None:
    """One corrupt record must never wedge the pass — the N1 vector was the
    abandoned_at read in the retention predicate: the healthy record still
    delivers, the corrupt one is kept and counted unreadable (task #3797)."""
    agent_id = _agent(db_conn)
    good = _record(agent_id=agent_id, content="deliver me", key="key-good", now=_NOW)
    corrupt = _record(agent_id=agent_id, content="corrupt", key="key-bad", now=_NOW)
    assert good is not None and corrupt is not None
    entry = outbox._read(corrupt)
    assert entry is not None
    outbox._abandon(corrupt, entry, "budget", _NOW)
    raw = json.loads(corrupt.read_text())
    raw["abandoned_at"] = "garbage"
    corrupt.write_text(json.dumps(raw))
    report = outbox.flush(pool, now=_NOW + timedelta(seconds=31))
    assert report.delivered == 1 and report.unreadable == 1
    assert not good.exists() and corrupt.exists()


def test_read_defaults_absent_abandon_detail(journal: Path) -> None:
    """Records written before the detail field (schema stays 1) still parse:
    the missing key reads as None instead of turning the record unreadable."""
    path = _record(agent_id=7, now=_NOW)
    assert path is not None
    raw = json.loads(path.read_text())
    del raw["abandon_detail"]
    path.write_text(json.dumps(raw))
    entry = outbox._read(path)
    assert entry is not None and entry.abandon_detail is None
