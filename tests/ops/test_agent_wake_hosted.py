"""Hosted-mode lifecycle ops — the same wake functions, minus the process.

Process mode: resurrect / respawn / swap-in / revive all end in a detached
process launch (`ops/agent_launch`). Hosted mode: the row flip IS the op —
the dispatcher owns delivery, and the op's only extra duty is the explicit
Redis wake (the inbound INSERTs inside these functions are raw SQL and do not
publish). These tests pin that contract: the status transition and the
inbound rows are untouched, the launch machinery is never called, and the
wake is published exactly once.

Locked here because the failure mode is not a crash: a hosted resurrect that
silently forked a process would double-claim the same inbound with the
dispatcher's turn task, and a missing wake would leave a flipped row pending
forever.
"""

from __future__ import annotations

from collections.abc import Iterator

import psycopg
import pytest

from ops import agent_revive, agent_wake
from ops.agent_spawn import create_agent_row
from shared.machine import machine_name

_DEAD_PID = 424243


@pytest.fixture(autouse=True)
def _hosted_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(agent_wake.runner_mode, "is_hosted", lambda: True)


@pytest.fixture(autouse=True)
def _guard_process_launch(monkeypatch: pytest.MonkeyPatch) -> None:
    """Hosted mode must never reach the process launchers — turn every launch
    entry point into a loud failure so a regression fails fast instead of
    forking a real child in the test suite."""

    def _boom(*_a: object, **_k: object) -> object:
        raise AssertionError("process launch reached in hosted mode")

    monkeypatch.setattr(agent_wake.agent_launch, "_launch_agent_process", _boom)
    monkeypatch.setattr(agent_wake.agent_launch, "_launch_or_force_terminated", _boom)
    monkeypatch.setattr(agent_wake.agent_launch, "_kill_stale_session", _boom)
    monkeypatch.setattr(agent_wake.agent_launch, "_wait_for_agent_claim", _boom)


@pytest.fixture
def wakes() -> Iterator[list[tuple[int, str]]]:
    captured: list[tuple[int, str]] = []
    yield captured


@pytest.fixture(autouse=True)
def _capture_wakes(monkeypatch: pytest.MonkeyPatch, wakes: list[tuple[int, str]]) -> Iterator[None]:
    def _record(agent_id: int, payload: str) -> None:
        wakes.append((agent_id, payload))

    # Both wake modules publish through their own namespace (the split moved
    # swap-in / revive to `ops.agent_revive`), so capture both.
    monkeypatch.setattr(agent_wake, "publish_inbound_wake", _record)
    monkeypatch.setattr(agent_revive, "publish_inbound_wake", _record)
    yield


def _park(
    db: psycopg.Connection,
    *,
    status: str,
    pid: int | None = None,
) -> int:
    """Seed a row WITHOUT any launch — hosted spawn is row-only, and the
    guard above turns a stray process launch into a loud failure."""
    aid, _birth = create_agent_row(spawner="user", machine=machine_name())
    with db.cursor() as cur:
        cur.execute("UPDATE agents_meta SET status=%s, pid=%s WHERE id=%s", (status, pid, aid))
    db.commit()
    return aid


def _row(db: psycopg.Connection, aid: int) -> tuple[str, int | None]:
    with db.cursor() as cur:
        cur.execute("SELECT status, pid FROM agents_meta WHERE id = %s", (aid,))
        row = cur.fetchone()
    assert row is not None, f"agents_meta row {aid} missing"
    return row[0], row[1]


def _kind_rows(db: psycopg.Connection, aid: int, kind: str) -> int:
    with db.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM inbound_messages WHERE agent_id = %s AND kind = %s",
            (aid, kind),
        )
        row = cur.fetchone()
    assert row is not None, f"inbound count row for agent {aid} missing"
    return row[0]


# ── resurrect ────────────────────────────────────────────────────────────────


def test_resurrect_agent_hosted_flips_and_wakes(
    db_conn: psycopg.Connection, wakes: list[tuple[int, str]]
) -> None:
    """terminated -> idling + resurrect inbound + one wake; no launch, no
    pid-confirm polling."""
    aid = _park(db_conn, status="terminated")
    out = agent_wake.resurrect_agent(aid, resurrected_by="user")
    assert out == aid
    assert _row(db_conn, aid) == ("idling", None)
    assert _kind_rows(db_conn, aid, "resurrect") == 1
    assert wakes == [(aid, "0")]


def test_resurrect_agent_hosted_keeps_trigger_guard(
    db_conn: psycopg.Connection, wakes: list[tuple[int, str]]
) -> None:
    """The auto-resurrect trigger CAS semantics are mode-independent: a stale
    trigger still refuses the transition."""
    aid = _park(db_conn, status="terminated")
    with pytest.raises(agent_wake.ResurrectTriggerStaleError):
        agent_wake.resurrect_agent(
            aid,
            resurrected_by="system",
            trigger_inbound_id=999999,
            trigger_inbound_kind="chat",
        )
    assert _row(db_conn, aid)[0] == "terminated"
    assert wakes == []


# ── respawn ──────────────────────────────────────────────────────────────────


def test_respawn_agent_hosted_flips_and_wakes(
    db_conn: psycopg.Connection, wakes: list[tuple[int, str]]
) -> None:
    """restarting -> idling + restart_completed inbound + one wake (defensive:
    hosted restart never flips to 'restarting', but if a row lands here the
    hosted answer must not fork)."""
    aid = _park(db_conn, status="restarting")
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO inbound_messages (agent_id, content, kind, source) "
            "VALUES (%s, '', 'restart', 'self')",
            (aid,),
        )
    db_conn.commit()
    assert agent_wake.respawn_agent(aid) is True
    assert _row(db_conn, aid) == ("idling", None)
    assert _kind_rows(db_conn, aid, "restart_completed") == 1
    assert wakes == [(aid, "0")]


# ── swap-in ──────────────────────────────────────────────────────────────────


# ── revive ───────────────────────────────────────────────────────────────────


def test_revive_agent_hosted_flips_and_wakes(
    db_conn: psycopg.Connection, wakes: list[tuple[int, str]]
) -> None:
    """running-with-dead-pid -> idling + one wake, no process (defensive: the
    hosted restarter is gated off, but a stale row must never fork)."""
    aid = _park(db_conn, status="running", pid=_DEAD_PID)
    assert agent_wake.revive_agent(aid, _DEAD_PID) is True
    assert _row(db_conn, aid) == ("idling", None)
    assert wakes == [(aid, "0")]
