"""`shared.watcher_registry` — the agent_watchers table CRUD.

The registry is the "should it exist?" half of the R1 watcher frame (Task
#1021): `ava.watcher.at/cron/launch` writes a row at spawn; a clean-exiting
watcher deletes its own row; the boot reconcile reads rows whose session is
gone to rebuild / mark. These tests exercise the table operations against the
test DB (the migration is applied by conftest like every other migration).
"""

from __future__ import annotations

import datetime
from collections.abc import Iterator

import psycopg
import pytest

from shared import watcher_registry as wr
from shared.config import settings
from shared.test_db_guard import assert_test_db_url

_FUTURE = datetime.datetime.now(datetime.UTC) + datetime.timedelta(hours=1)
_WATCHER_AGENT_IDS = (1, 2, 7, 42)


def _seed_agents(*agent_ids: int) -> None:
    """Create the agent rows required by the watcher registry FK."""
    from ava._settings import DB_URL

    assert_test_db_url(str(DB_URL), context="test_watcher_registry._seed_agents")
    with psycopg.connect(DB_URL, autocommit=True) as conn:
        for agent_id in agent_ids:
            conn.execute(
                "INSERT INTO agents (id) VALUES (%s) ON CONFLICT (id) DO NOTHING",
                (agent_id,),
            )


@pytest.fixture(autouse=True)
def _clean_watcher_rows() -> Iterator[None]:
    """agent_watchers is not in conftest's TRUNCATE list (it is a registry, not
    business data), so clear it before and after each test."""
    with psycopg.connect(settings.data_plane.db_url, autocommit=True) as conn:
        conn.execute("DELETE FROM agent_watchers")
    _seed_agents(*_WATCHER_AGENT_IDS)
    yield
    with psycopg.connect(settings.data_plane.db_url, autocommit=True) as conn:
        conn.execute("DELETE FROM agent_watchers")


def test_register_rejects_nonexistent_agent() -> None:
    """An orphan watcher is rejected instead of silently entering the registry."""
    with (
        psycopg.connect(settings.data_plane.db_url, autocommit=True) as conn,
        pytest.raises(psycopg.errors.ForeignKeyViolation),
    ):
        conn.execute(
            "INSERT INTO agent_watchers (session_id, agent_id, kind, name) "
            "VALUES (1, 999_999, 'at', 'orphan')"
        )


def test_register_and_read_roundtrip() -> None:
    wr.register_watcher(
        42,
        1001,
        kind="cron",
        name="daily-check",
        message="daily stand-up",
        cron_expr="0 9 * * *",
        cron_timezone="America/Los_Angeles",
    )
    rows = wr.watcher_rows(agent_id=42)
    assert len(rows) == 1
    row = rows[0]
    assert row["session_id"] == 1001
    assert row["agent_id"] == 42
    assert row["kind"] == "cron"
    assert row["name"] == "daily-check"
    assert row["cron_expr"] == "0 9 * * *"
    assert row["status"] == "running"
    assert row["cron_end_at"] is None
    # other agents are not returned
    assert wr.watcher_rows(agent_id=43) == []
    assert wr.watcher_rows(agent_id=42) == [row]
    # session-id view
    assert wr.watcher_session_ids(agent_id=42) == {1001}


def test_register_kinds_and_payloads() -> None:
    wr.register_watcher(7, 1, kind="at", name="one-shot", message="go", fires_at=_FUTURE)
    wr.register_watcher(7, 2, kind="launch", name="ci-poll", timeout_secs=5400.0)
    rows = {r["session_id"]: r for r in wr.watcher_rows(agent_id=7)}
    assert rows[1]["fires_at"] is not None and rows[1]["cron_expr"] is None
    assert rows[2]["timeout_secs"] == 5400.0 and rows[2]["message"] is None


def test_register_conflict_is_noop() -> None:
    wr.register_watcher(1, 5, kind="cron", name="a", cron_expr="* * * * *")
    wr.register_watcher(1, 5, kind="launch", name="b", timeout_secs=1.0)
    rows = wr.watcher_rows(agent_id=1)
    assert len(rows) == 1
    assert rows[0]["name"] == "a"  # first write wins


def test_register_rejects_unknown_kind() -> None:
    with pytest.raises(ValueError, match="unknown watcher kind"):
        wr.register_watcher(1, 9, kind="bogus", name="x")


def test_delete_watcher_removes_row() -> None:
    wr.register_watcher(1, 3, kind="at", name="s", message="m", fires_at=_FUTURE)
    wr.delete_watcher(1, 3)
    assert wr.watcher_rows(agent_id=1) == []
    wr.delete_watcher(1, 3)  # missing row is a no-op


def test_mark_status_transitions() -> None:
    wr.register_watcher(1, 4, kind="cron", name="c", cron_expr="* * * * *")
    wr.mark_status(1, 4, "rebuilt")
    assert wr.watcher_rows(agent_id=1)[0]["status"] == "rebuilt"
    wr.mark_status(1, 4, "missed")
    assert wr.watcher_rows(agent_id=1)[0]["status"] == "missed"
    with pytest.raises(ValueError, match="unknown watcher status"):
        wr.mark_status(1, 4, "bogus")


def test_same_session_id_different_agents_coexist() -> None:
    """Task #1155 (P0): session ids are PER-AGENT counters
    (agents_meta.session_index) — two agents may both hold session 5. The
    registry keys rows by (agent_id, session_id), so both must register and
    delete independently; a delete for one agent must never touch the
    other's same-numbered row."""
    wr.register_watcher(1, 5, kind="cron", name="agent1-cron", cron_expr="* * * * *")
    wr.register_watcher(2, 5, kind="cron", name="agent2-cron", cron_expr="* * * * *")

    rows = wr.watcher_rows()
    assert len(rows) == 2  # both rows exist — no silent ON CONFLICT drop
    by_agent = {r["agent_id"]: r["name"] for r in rows}
    assert by_agent == {1: "agent1-cron", 2: "agent2-cron"}

    # deleting agent 1's row leaves agent 2's same-numbered row intact
    wr.delete_watcher(1, 5)
    assert wr.watcher_rows(agent_id=1) == []
    assert wr.watcher_rows(agent_id=2) == [rows[1] if rows[1]["agent_id"] == 2 else rows[0]]


def test_mark_status_scoped_to_agent() -> None:
    """mark_status must transition only the given agent's row — a same
    session_id held by another agent must not be touched (#1155)."""
    wr.register_watcher(1, 6, kind="cron", name="a1", cron_expr="* * * * *")
    wr.register_watcher(2, 6, kind="cron", name="a2", cron_expr="* * * * *")
    wr.mark_status(1, 6, "rebuilt")
    rows = {r["agent_id"]: r["status"] for r in wr.watcher_rows()}
    assert rows == {1: "rebuilt", 2: "running"}


# ─── atomic cron registration (Task #1825 N2) ────────────────────────────────


def test_register_cron_atomic_inserts_when_no_live_duplicate() -> None:
    """No same-schedule live row → the new row is inserted and committed."""
    reused = wr.register_cron_atomic(
        42,
        2001,
        name="daily",
        message="stand-up",
        cron_expr="0 9 * * *",
        cron_timezone="UTC",
        cron_end_at=None,
        alive_provider=set,
    )
    assert reused is None
    rows = wr.watcher_rows(agent_id=42)
    assert len(rows) == 1
    assert rows[0]["session_id"] == 2001
    assert rows[0]["status"] == "running"


def test_register_cron_atomic_reuses_live_duplicate() -> None:
    """A same-schedule row whose session is LIVE is reused: no new row is
    inserted, the existing session id is returned."""
    wr.register_watcher(
        42,
        2002,
        kind="cron",
        name="daily",
        message="x",
        cron_expr="0 9 * * *",
        cron_timezone="UTC",
    )
    reused = wr.register_cron_atomic(
        42,
        2003,
        name="daily-dup",
        message="x",
        cron_expr="0 9 * * *",
        cron_timezone="UTC",
        cron_end_at=None,
        alive_provider=lambda: {2002},
    )
    assert reused == 2002
    rows = wr.watcher_rows(agent_id=42)
    assert [r["session_id"] for r in rows] == [2002]  # 2003 never inserted


def test_register_cron_atomic_ignores_dead_duplicate() -> None:
    """A same-schedule row whose session is DEAD does not block the insert —
    that dead row is exactly what the boot reconcile is about to rebuild."""
    wr.register_watcher(
        42,
        2004,
        kind="cron",
        name="daily",
        message="x",
        cron_expr="0 9 * * *",
        cron_timezone="UTC",
    )
    reused = wr.register_cron_atomic(
        42,
        2005,
        name="daily",
        message="x",
        cron_expr="0 9 * * *",
        cron_timezone="UTC",
        cron_end_at=None,
        alive_provider=set,  # 2004's session is gone
    )
    assert reused is None
    rows = {r["session_id"] for r in wr.watcher_rows(agent_id=42)}
    assert rows == {2004, 2005}


def test_register_cron_atomic_schedule_scoped() -> None:
    """The dedupe key is the full schedule: a different timezone or end time
    is a different watcher and does not block; a standing cron (NULL end) and
    an ended one do not collide."""
    wr.register_watcher(
        42,
        2006,
        kind="cron",
        name="utc",
        message="x",
        cron_expr="0 9 * * *",
        cron_timezone="UTC",
    )
    # same expr, different timezone — separate watcher
    reused = wr.register_cron_atomic(
        42,
        2007,
        name="sh",
        message="x",
        cron_expr="0 9 * * *",
        cron_timezone="Asia/Shanghai",
        cron_end_at=None,
        alive_provider=lambda: {2006},
    )
    assert reused is None
    # same timezone, different end time — separate watcher
    reused = wr.register_cron_atomic(
        42,
        2008,
        name="ended",
        message="x",
        cron_expr="0 9 * * *",
        cron_timezone="UTC",
        cron_end_at=_FUTURE,
        alive_provider=lambda: {2006},
    )
    assert reused is None
    rows = {r["session_id"] for r in wr.watcher_rows(agent_id=42)}
    assert rows == {2006, 2007, 2008}


def test_register_cron_atomic_exclude_session() -> None:
    """exclude_session skips one live row — the stale-template rebuild must
    replace (not dedupe against) the very session it is upgrading."""
    wr.register_watcher(
        42,
        2009,
        kind="cron",
        name="stale",
        message="x",
        cron_expr="0 9 * * *",
        cron_timezone="UTC",
    )
    reused = wr.register_cron_atomic(
        42,
        2010,
        name="stale",
        message="x",
        cron_expr="0 9 * * *",
        cron_timezone="UTC",
        cron_end_at=None,
        alive_provider=lambda: {2009},
        exclude_session=2009,
    )
    assert reused is None  # the live 2009 was excluded — 2010 inserted
    rows = {r["session_id"] for r in wr.watcher_rows(agent_id=42)}
    assert rows == {2009, 2010}


def test_register_cron_atomic_calls_alive_provider_under_lock() -> None:
    """The alive provider runs INSIDE the lock (QA nit, #794 delta2): at the
    moment it is called, the registration transaction holds the schedule's
    advisory xact lock — visible in pg_locks from a separate connection. A
    snapshot taken before the lock would miss a concurrent winner's session."""
    from shared.db import connect as _connect

    observed: list[bool] = []

    def provider() -> set[int]:
        with _connect() as conn:
            row = conn.execute(
                "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND granted"
            ).fetchone()
        assert row is not None
        observed.append(int(row[0]) >= 1)
        return set()

    wr.register_cron_atomic(
        42,
        2100,
        name="locked",
        message="x",
        cron_expr="0 9 * * *",
        cron_timezone="UTC",
        cron_end_at=None,
        alive_provider=provider,
    )
    assert observed == [True], "alive provider was not called under the lock"


def test_register_cron_atomic_exclude_session_zero() -> None:
    """exclude_session=0 (an agent's very first session — session_index
    starts at 0) must exclude row 0, not degrade to the -1 sentinel (QA nit,
    #794 delta2): the stale-template rebuild of the first watcher must
    replace it, not dedupe against it."""
    wr.register_watcher(
        42,
        0,
        kind="cron",
        name="first",
        message="x",
        cron_expr="0 9 * * *",
        cron_timezone="UTC",
    )
    reused = wr.register_cron_atomic(
        42,
        2011,
        name="first",
        message="x",
        cron_expr="0 9 * * *",
        cron_timezone="UTC",
        cron_end_at=None,
        alive_provider=lambda: {0},
        exclude_session=0,
    )
    assert reused is None  # row 0 excluded → 2011 inserted
    rows = {r["session_id"] for r in wr.watcher_rows(agent_id=42)}
    assert rows == {0, 2011}
