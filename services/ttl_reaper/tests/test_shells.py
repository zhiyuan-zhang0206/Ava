"""Shell reclamation against the real Postgres fixture: expired shell rows
dispatch a shell_kill op to their home machine and are deleted only on a
definitive verdict (killed / absent / machine_absent) — unreachable machines,
failed ops and unknown machines leave the row for the next round. Owners are
notified only when the reap interrupted a running job; an idle or already-absent
shell's reaping is silent. A renewal between the select and the dispatch wins."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import psycopg
import pytest
from psycopg_pool import ConnectionPool

from base.daemon.loop_health import LoopProgress
from base.db import create_agent
from ops.rpc_schemas import ShellKillResult
from services.ttl_reaper import shells
from services.ttl_reaper.shells import _claim_shell_row_still_expired, reap_expired_shells


def _progress() -> LoopProgress:
    return LoopProgress("test", 60.0)


async def _reap(pool: ConnectionPool) -> list[tuple[int, int]]:
    return await reap_expired_shells(pool, _progress())


@pytest.fixture()
def reaper_pool() -> Iterator[ConnectionPool]:
    """The reaper's own small pool (the functions take a ConnectionPool)."""
    import base.db

    pool = base.db.pool(max_size=2)
    yield pool
    pool.close()


def _running_agent(conn: psycopg.Connection) -> int:
    aid = create_agent(conn)
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO agents_meta (id, spawner, status) VALUES (%s, 'user', 'running')",
            (aid,),
        )
    conn.commit()
    return aid


def _system_inbounds(conn: psycopg.Connection, agent_id: int) -> list[str]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT content FROM inbound_messages WHERE agent_id = %s AND source = 'system'",
            (agent_id,),
        )
        return [r[0] for r in cur.fetchall()]


def _insert_shell_row(
    db_conn: psycopg.Connection,
    agent_id: int,
    session_id: int,
    *,
    expires_at: datetime,
) -> None:
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO agent_shell_ttls (agent_id, session_id, expires_at, created_at) "
            "VALUES (%s, %s, %s, now() - interval '1 hour') "
            "ON CONFLICT (agent_id, session_id) DO UPDATE SET expires_at = EXCLUDED.expires_at",
            (agent_id, session_id, expires_at),
        )
    db_conn.commit()


async def test_reap_expired_shells_deletes_on_killed(
    db_conn: psycopg.Connection, reaper_pool: ConnectionPool, monkeypatch: pytest.MonkeyPatch
) -> None:
    aid = _running_agent(db_conn)
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO agent_shell_ttls (agent_id, session_id, expires_at, created_at) "
            "VALUES (%s, 3, now() - interval '1 minute', now() - interval '1 hour 1 minute')",
            (aid,),
        )
        cur.execute("UPDATE agents_meta SET machine = 'macmini' WHERE id = %s", (aid,))
    db_conn.commit()

    async def _dispatch(
        machine: str, kind: str, payload: dict[str, object], **kwargs: object
    ) -> dict[str, object]:
        assert machine == "macmini"
        assert kind == "shell_kill"
        assert payload == {"agent_id": aid, "session_id": 3}
        return ShellKillResult(mode="killed", interrupted=True, name="build").model_dump()

    monkeypatch.setattr(
        shells.cluster_rpc,
        "dispatch_to_machine",
        _dispatch,
    )
    reaped = await _reap(reaper_pool)

    assert reaped == [(aid, 3)]
    with db_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM agent_shell_ttls WHERE agent_id = %s", (aid,))
        row = cur.fetchone()
        assert row is not None and row[0] == 0
    msgs = _system_inbounds(db_conn, aid)
    assert len(msgs) == 1
    assert "Shell session 'build' (id 3, agent" in msgs[0]
    assert "expired at " in msgs[0] and "TTL 1h" in msgs[0], msgs[0]
    assert "interrupting a running task" in msgs[0]


async def test_reap_expired_shells_keeps_row_on_unreachable(
    db_conn: psycopg.Connection, reaper_pool: ConnectionPool, monkeypatch: pytest.MonkeyPatch
) -> None:
    aid = _running_agent(db_conn)
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO agent_shell_ttls (agent_id, session_id, expires_at) "
            "VALUES (%s, 7, now() - interval '1 minute')",
            (aid,),
        )
        cur.execute("UPDATE agents_meta SET machine = 'macmini' WHERE id = %s", (aid,))
    db_conn.commit()

    async def _dispatch(
        machine: str, kind: str, payload: dict[str, object], **kwargs: object
    ) -> dict[str, object]:
        raise shells.cluster_rpc.ClusterOpUnreachable("boom")

    monkeypatch.setattr(
        shells.cluster_rpc,
        "dispatch_to_machine",
        _dispatch,
    )
    reaped = await _reap(reaper_pool)

    assert reaped == []
    with db_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM agent_shell_ttls WHERE agent_id = %s", (aid,))
        row = cur.fetchone()
        assert row is not None and row[0] == 1


async def test_reap_expired_shells_absent_machine_terminalizes_row(
    db_conn: psycopg.Connection, reaper_pool: ConnectionPool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Task #4143: a machine absent from the registry is a definitive verdict —
    the expired row is dropped as an absent session instead of being deferred
    forever, and nothing notifies (nothing on a gone machine was interrupted)."""
    aid = _running_agent(db_conn)
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO agent_shell_ttls (agent_id, session_id, expires_at, created_at) "
            "VALUES (%s, 8, now() - interval '1 minute', now() - interval '1 hour 1 minute')",
            (aid,),
        )
        cur.execute("UPDATE agents_meta SET machine = 'ghost' WHERE id = %s", (aid,))
    db_conn.commit()

    async def _dispatch(
        machine: str, kind: str, payload: dict[str, object], **kwargs: object
    ) -> dict[str, object]:
        raise shells.cluster_rpc.ClusterOpTargetAbsent(
            "machine 'ghost' is absent from the machines registry"
        )

    monkeypatch.setattr(shells.cluster_rpc, "dispatch_to_machine", _dispatch)
    emitted: list[tuple[tuple[object, ...], dict[str, object]]] = []

    def _capture_emit(*args: object, **kwargs: object) -> None:
        emitted.append((args, kwargs))

    monkeypatch.setattr(shells.telemetry, "emit", _capture_emit)
    reaped = await _reap(reaper_pool)

    assert reaped == [(aid, 8)]
    with db_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM agent_shell_ttls WHERE agent_id = %s", (aid,))
        row = cur.fetchone()
        assert row is not None and row[0] == 0
    assert _system_inbounds(db_conn, aid) == []
    # The synthetic verdict is spelled ``machine_absent`` — distinct from the
    # live-host ``absent`` (session already ended) it must not masquerade as.
    assert emitted and emitted[-1][0][1] == "shell_ttl_expired"
    assert emitted[-1][1]["attributes"] == {
        "agent_id": aid,
        "session_id": 8,
        "mode": "machine_absent",
        "interrupted": False,
    }


async def test_reap_expired_shells_keeps_row_on_unknown_machine(
    db_conn: psycopg.Connection, reaper_pool: ConnectionPool
) -> None:
    aid = _running_agent(db_conn)
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO agent_shell_ttls (agent_id, session_id, expires_at) "
            "VALUES (%s, 9, now() - interval '1 minute')",
            (aid,),
        )
    db_conn.commit()

    reaped = await _reap(reaper_pool)
    assert reaped == []
    with db_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM agent_shell_ttls WHERE agent_id = %s", (aid,))
        row = cur.fetchone()
        assert row is not None and row[0] == 1


async def test_reap_expired_shells_idle_reaping_is_silent(
    db_conn: psycopg.Connection, reaper_pool: ConnectionPool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A killed shell that carried no running job is reclaimed without a
    notice — the user ruling: only a reap that interrupts running work
    messages the owner."""
    aid = _running_agent(db_conn)
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO agent_shell_ttls (agent_id, session_id, expires_at, created_at) "
            "VALUES (%s, 4, now() - interval '1 minute', now() - interval '31 minutes')",
            (aid,),
        )
        cur.execute("UPDATE agents_meta SET machine = 'macmini' WHERE id = %s", (aid,))
    db_conn.commit()

    async def _dispatch(
        machine: str, kind: str, payload: dict[str, object], **kwargs: object
    ) -> dict[str, object]:
        return ShellKillResult(mode="killed", interrupted=False).model_dump()

    monkeypatch.setattr(
        shells.cluster_rpc,
        "dispatch_to_machine",
        _dispatch,
    )
    reaped = await _reap(reaper_pool)

    assert reaped == [(aid, 4)]
    with db_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM agent_shell_ttls WHERE agent_id = %s", (aid,))
        row = cur.fetchone()
        assert row is not None and row[0] == 0
    assert _system_inbounds(db_conn, aid) == []


async def test_reap_expired_shells_absent_reaping_is_silent(
    db_conn: psycopg.Connection, reaper_pool: ConnectionPool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A session already gone was not interrupted — reclaiming its row sends
    no notice (previously it notified; the 2026-08-27 ruling narrows shell
    notices to interrupted work)."""
    aid = _running_agent(db_conn)
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO agent_shell_ttls (agent_id, session_id, expires_at) "
            "VALUES (%s, 5, now() - interval '1 minute')",
            (aid,),
        )
        cur.execute("UPDATE agents_meta SET machine = 'macmini' WHERE id = %s", (aid,))
    db_conn.commit()

    async def _dispatch(
        machine: str, kind: str, payload: dict[str, object], **kwargs: object
    ) -> dict[str, object]:
        return ShellKillResult(mode="absent").model_dump()

    monkeypatch.setattr(
        shells.cluster_rpc,
        "dispatch_to_machine",
        _dispatch,
    )
    reaped = await _reap(reaper_pool)

    assert reaped == [(aid, 5)]
    with db_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM agent_shell_ttls WHERE agent_id = %s", (aid,))
        row = cur.fetchone()
        assert row is not None and row[0] == 0
    assert _system_inbounds(db_conn, aid) == []


async def test_reap_expired_shells_missing_interrupted_field_notifies(
    db_conn: psycopg.Connection, reaper_pool: ConnectionPool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pre-policy runner's shell_kill result has no `interrupted` field —
    default to notifying (the old behavior) so a version-skewed fleet never
    silently swallows an interruption notice."""
    aid = _running_agent(db_conn)
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO agent_shell_ttls (agent_id, session_id, expires_at) "
            "VALUES (%s, 6, now() - interval '1 minute')",
            (aid,),
        )
        cur.execute("UPDATE agents_meta SET machine = 'macmini' WHERE id = %s", (aid,))
    db_conn.commit()

    async def _dispatch(
        machine: str, kind: str, payload: dict[str, object], **kwargs: object
    ) -> dict[str, object]:
        return {"mode": "killed"}  # old runner: no interrupted field

    monkeypatch.setattr(
        shells.cluster_rpc,
        "dispatch_to_machine",
        _dispatch,
    )
    reaped = await _reap(reaper_pool)

    assert reaped == [(aid, 6)]
    msgs = _system_inbounds(db_conn, aid)
    assert len(msgs) == 1 and "interrupting a running task" in msgs[0]


async def test_reap_expired_shells_notifies_for_a_watcher_shaped_session(
    db_conn: psycopg.Connection, reaper_pool: ConnectionPool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A watcher (`ava.watcher.at/cron/launch`) is just an `agent_shell_ttls`
    row with no registry of its own (decisions/2026-09-27-watchers-are-never-
    restarted.md): there is no special case left in this pass, so a reclaimed
    watcher-shaped session — one that was actually running a script, exactly
    like a real watcher always is at its TTL deadline — gets the SAME
    interruption notice any other reclaimed shell with live work gets, and
    exactly once."""
    aid = _running_agent(db_conn)
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO agent_shell_ttls (agent_id, session_id, expires_at, created_at) "
            "VALUES (%s, 7, now() - interval '1 minute', now() - interval '31 minutes')",
            (aid,),
        )
        cur.execute("UPDATE agents_meta SET machine = 'macmini' WHERE id = %s", (aid,))
    db_conn.commit()

    async def _dispatch(
        machine: str, kind: str, payload: dict[str, object], **kwargs: object
    ) -> dict[str, object]:
        # A watcher's session always has a running job (its generated script,
        # sleeping toward its next fire) — the runner reports it as such.
        return ShellKillResult(mode="killed", interrupted=True, name="test-watcher").model_dump()

    monkeypatch.setattr(shells.cluster_rpc, "dispatch_to_machine", _dispatch)
    reaped = await _reap(reaper_pool)

    assert reaped == [(aid, 7)]
    with db_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM agent_shell_ttls WHERE agent_id = %s", (aid,))
        row = cur.fetchone()
        assert row is not None and row[0] == 0
    msgs = _system_inbounds(db_conn, aid)
    assert len(msgs) == 1
    assert "test-watcher" in msgs[0] and "interrupting a running task" in msgs[0]


async def test_reap_expired_shells_skips_row_renewed_after_select(
    db_conn: psycopg.Connection, reaper_pool: ConnectionPool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The race the claim closes: the expired-row select ran first, then the
    owner renewed (deadline now in the future). The pass must not kill —
    the stale select's row fails the claim and the live row survives."""
    aid = _running_agent(db_conn)
    _insert_shell_row(db_conn, aid, 44, expires_at=datetime.now(UTC) - timedelta(minutes=1))
    with db_conn.cursor() as cur:
        cur.execute("UPDATE agents_meta SET machine = 'macmini' WHERE id = %s", (aid,))
        # The renewal that landed after the (stale) select: deadline moved forward.
        cur.execute(
            "UPDATE agent_shell_ttls SET expires_at = now() + interval '1 hour', "
            "renewals = renewals + 1 WHERE agent_id = %s AND session_id = %s",
            (aid, 44),
        )
    db_conn.commit()

    stale_rows: list[dict[str, object]] = [
        {
            "agent_id": aid,
            "session_id": 44,
            "machine": "macmini",
            "expires_at": datetime.now(UTC) - timedelta(minutes=1),
            "created_at": datetime.now(UTC) - timedelta(hours=2),
        }
    ]

    def _stale_select(_pool: ConnectionPool) -> list[dict[str, object]]:
        return stale_rows

    monkeypatch.setattr(shells, "_expired_shell_rows_blocking", _stale_select)
    dispatched: list[tuple[str, object]] = []

    async def _dispatch(
        machine: str, kind: str, payload: dict[str, object], **kwargs: object
    ) -> dict[str, object]:
        dispatched.append((kind, payload))
        return ShellKillResult(mode="killed", interrupted=True, name="x").model_dump()

    monkeypatch.setattr(shells.cluster_rpc, "dispatch_to_machine", _dispatch)
    reaped = await _reap(reaper_pool)

    assert reaped == []
    assert dispatched == []
    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM agent_shell_ttls WHERE agent_id = %s AND session_id = %s",
            (aid, 44),
        )
        row = cur.fetchone()
        assert row is not None and row[0] == 1


def test_claim_still_expired_true_for_expired_row(
    db_conn: psycopg.Connection, reaper_pool: ConnectionPool
) -> None:
    aid = _running_agent(db_conn)
    _insert_shell_row(db_conn, aid, 41, expires_at=datetime.now(UTC) - timedelta(minutes=1))
    assert _claim_shell_row_still_expired(reaper_pool, aid, 41)


def test_claim_still_expired_false_for_renewed_row(
    db_conn: psycopg.Connection, reaper_pool: ConnectionPool
) -> None:
    """A row renewed between the expired select and the kill dispatch fails
    the claim — the deadline moved forward, no kill may be dispatched."""
    aid = _running_agent(db_conn)
    _insert_shell_row(db_conn, aid, 42, expires_at=datetime.now(UTC) + timedelta(hours=1))
    assert not _claim_shell_row_still_expired(reaper_pool, aid, 42)


def test_claim_still_expired_evaluates_at_statement_time(
    db_conn: psycopg.Connection,
) -> None:
    """Issue #2053: the claim re-check reads the deadline at statement time.

    A claim transaction that began before the deadline still claims the row
    once the deadline has actually passed; with now() (transaction start) it
    would skip the row and the pass would never dispatch the kill.
    """
    import time

    aid = _running_agent(db_conn)
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO agent_shell_ttls (agent_id, session_id, expires_at, created_at) "
            "VALUES (%s, %s, clock_timestamp() + interval '1 second', clock_timestamp())",
            (aid, 45),
        )
    db_conn.commit()
    with db_conn.cursor() as cur:
        # The transaction starts before the deadline passes.
        cur.execute("SET TRANSACTION READ WRITE")
        time.sleep(2.0)
        assert shells._claim_still_expired(cur, aid, 45)
    db_conn.rollback()


def test_claim_still_expired_false_for_missing_row(
    db_conn: psycopg.Connection, reaper_pool: ConnectionPool
) -> None:
    aid = _running_agent(db_conn)
    assert not _claim_shell_row_still_expired(reaper_pool, aid, 43)


def test_human_ttl_formats_compact_durations() -> None:
    """The notice's TTL duration reads compactly: whole hours/months as h/m,
    anything else in seconds; negative (clock-skewed) durations clamp to 0s."""
    from services.ttl_reaper.shells import _human_ttl

    assert _human_ttl(3600) == "1h"
    assert _human_ttl(2 * 3600) == "2h"
    assert _human_ttl(1800) == "30m"
    assert _human_ttl(90) == "90s"
    assert _human_ttl(0) == "0s"
    assert _human_ttl(-61) == "0s"


def test_wall_clock_renders_cluster_timezone(monkeypatch: pytest.MonkeyPatch) -> None:
    """The notice's expiry time renders in the cluster timezone: bare HH:MM on
    the cluster's today, MM-DD HH:MM when the TTL crosses midnight."""
    from datetime import UTC, datetime, timedelta
    from zoneinfo import ZoneInfo

    sh = ZoneInfo("Asia/Shanghai")
    monkeypatch.setattr(shells, "cluster_tz", lambda: sh)
    now = datetime.now(sh)

    # A specific wall-clock moment on the cluster's today: the same instant
    # fed in as UTC must render the Shanghai wall clock, no date prefix.
    at = now.replace(hour=23, minute=59, second=0, microsecond=0)
    assert shells._wall_clock(at.astimezone(UTC)) == "23:59"

    # A moment on the cluster's tomorrow (TTL crossing midnight) carries the MM-DD prefix.
    cross = (now + timedelta(days=1)).replace(hour=0, minute=5, second=0, microsecond=0)
    assert shells._wall_clock(cross) == cross.strftime("%m-%d %H:%M")


def test_wall_clock_none_falls_back_to_host_zone(monkeypatch: pytest.MonkeyPatch) -> None:
    """cluster_tz() None is the host-zone fallback signal: the stamp matches
    dt.astimezone(None) (machine-local), not UTC."""
    from datetime import UTC, datetime

    monkeypatch.setattr(shells, "cluster_tz", lambda: None)
    # A moment on the host's today: the fallback stamp carries no date prefix.
    # (A fixed past date would cross midnight and gain the MM-DD prefix the
    # next day — a time-bomb assertion that expired 2026-08-28.)
    dt = (
        datetime.now()
        .astimezone()
        .replace(hour=12, minute=0, second=0, microsecond=0)
        .astimezone(UTC)
    )
    assert shells._wall_clock(dt) == dt.astimezone(None).strftime("%H:%M")
