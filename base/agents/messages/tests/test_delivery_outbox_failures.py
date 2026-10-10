"""Known database availability failures recover; unknown failures retain commit facts."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import psycopg
import pytest
from psycopg_pool import ConnectionPool, PoolClosed

from base.agents.messages import delivery_outbox as outbox
from base.config.service_read import ConfigAuthority
from base.db import create_agent

from .test_delivery_outbox_sender import authority as authority

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
    monkeypatch.setenv("AVA_HOME", str(tmp_path))
    yield tmp_path


@pytest.fixture()
def pool() -> Iterator[ConnectionPool]:
    from base import db

    p = db.pool(max_size=2)
    yield p
    p.close()


def _patch_limits(monkeypatch: pytest.MonkeyPatch, **overrides: object) -> None:
    snapshot = _limits(**overrides)

    def read_limits(_authority: ConfigAuthority) -> outbox.DeliveryOutboxLimits:
        return snapshot

    monkeypatch.setattr(outbox, "limits", read_limits)


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
    authority: ConfigAuthority,
    *,
    agent_id: int,
    source: str = "watcher:7",
    content: str = "the daily check fired",
    key: str = "key-1",
    now: datetime | None = None,
) -> Path | None:
    return outbox.record_failed_send(
        authority=authority,
        agent_id=agent_id,
        origin_agent_id=None,
        source=source,
        content=content,
        client_message_id=key,
        now=now,
    )


def _inbounds(db_conn: psycopg.Connection, agent_id: int) -> list[tuple[str, str, str]]:
    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT content, source, client_message_id FROM inbound_messages "
            "WHERE agent_id = %s ORDER BY id",
            (agent_id,),
        )
        return [(str(r[0]), str(r[1]), str(r[2])) for r in cur.fetchall()]


def test_unknown_postcommit_wake_error_preserves_receipt_and_journal(
    journal: Path,
    authority: ConfigAuthority,
    db_conn: psycopg.Connection,
    pool: ConnectionPool,
    publish_wake: Callable[[int, str], bool],
) -> None:
    agent_id = _agent(db_conn)
    path = _record(authority, agent_id=agent_id, key="committed-key", now=_NOW)
    assert path is not None
    before = path.read_bytes()
    error = RuntimeError("wake implementation bug")

    def broken_wake(_agent: int, _source: str) -> bool:
        raise error

    with pytest.raises(RuntimeError) as raised:
        outbox.flush(pool, broken_wake, authority=authority, now=_NOW + timedelta(seconds=43201))
    assert error in (raised.value, raised.value.__cause__)
    assert path.read_bytes() == before
    assert _inbounds(db_conn, agent_id) == [("the daily check fired", "watcher:7", "committed-key")]
    assert (
        outbox.flush(
            pool, publish_wake, authority=authority, now=_NOW + timedelta(seconds=43202)
        ).delivered
        == 1
    )
    assert len(_inbounds(db_conn, agent_id)) == 1
    assert not path.exists()


@pytest.mark.parametrize(
    "error", [psycopg.errors.QueryCanceled("query canceled"), PoolClosed("pool closed")]
)
def test_unknown_database_failure_preserves_journal(
    journal: Path,
    authority: ConfigAuthority,
    db_conn: psycopg.Connection,
    pool: ConnectionPool,
    monkeypatch: pytest.MonkeyPatch,
    publish_wake: Callable[[int, str], bool],
    error: psycopg.OperationalError,
) -> None:
    path = _record(authority, agent_id=_agent(db_conn), now=_NOW)
    assert path is not None
    before = path.read_bytes()

    def broken_delivery(*_args: object, authority: ConfigAuthority) -> int:
        raise error

    monkeypatch.setattr(outbox, "_deliver", broken_delivery)
    with pytest.raises(type(error)) as raised:
        outbox.flush(pool, publish_wake, authority=authority, now=_NOW + timedelta(seconds=43201))
    assert raised.value is error
    assert path.read_bytes() == before


def test_known_pool_timeout_retains_bounded_recovery(
    journal: Path,
    authority: ConfigAuthority,
    db_conn: psycopg.Connection,
    pool: ConnectionPool,
    monkeypatch: pytest.MonkeyPatch,
    publish_wake: Callable[[int, str], bool],
) -> None:
    agent_id = _agent(db_conn)
    path = _record(authority, agent_id=agent_id, now=_NOW)
    assert path is not None
    _patch_limits(monkeypatch, flush_interval_seconds=0.01)
    with pool.connection(), pool.connection():
        assert (
            outbox.flush(
                pool, publish_wake, authority=authority, now=_NOW + timedelta(seconds=31)
            ).deferred
            == 1
        )
    entry = outbox._read(path)
    assert entry is not None and entry.flush_attempts == 1
    assert (
        outbox.flush(
            pool, publish_wake, authority=authority, now=_NOW + timedelta(seconds=92)
        ).delivered
        == 1
    )
    assert len(_inbounds(db_conn, agent_id)) == 1
