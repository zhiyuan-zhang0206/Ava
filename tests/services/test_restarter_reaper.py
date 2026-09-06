"""Restarter dead-birth and dead-process reaper unit tests.

An unclaimed idling row has NULL ownership columns and is reaped by age. A
claimed running/idling row with a dead pid and no produced message is reaped
into the crash-resurrect backoff path. Post-message process deaths retain their
direct revive behavior.

The reapers' predicate is process IDENTITY, not liveness (`ops.agent_identity`),
so it is the identity probe that is monkeypatched here rather than a pid check —
tests do not depend on real pids. The distinction has teeth: a recycled pid is
alive without being the agent, and reading that as "still running" is what left
prod rows stranded indefinitely (issue #1123).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import LiteralString

import psycopg
import pytest
from psycopg_pool import ConnectionPool

from ops.agent_identity import AgentProcessIdentity
from ops.controllers import respawn as rd  # reapers moved here from the restarter daemon
from shared.config import settings
from shared.machine import machine_name
from tests.conftest import spawn_agent

_DEAD_PID = 424242


def _stub_probe(monkeypatch: pytest.MonkeyPatch, verdict: AgentProcessIdentity) -> None:
    """Force every pid the reapers probe to come back with `verdict`."""
    monkeypatch.setattr(rd, "probe_agent_process", lambda _pid, _agent_id: verdict)  # pyright: ignore[reportUnknownArgumentType]


@pytest.fixture
def sync_pool():
    pool = ConnectionPool(settings.data_plane.db_url, min_size=1, max_size=2, open=True)
    try:
        yield pool
    finally:
        pool.close()


@pytest.fixture(autouse=True)
def _host_is_serving(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Reaper cases model a host that already passed its start gate."""
    from shared import start_serving

    monkeypatch.setattr(start_serving, "state_path", lambda: tmp_path / "start-serving.json")
    generation = start_serving.begin_start()
    assert start_serving.mark_serving(generation) is True


def _make_dead_boot_phase_agent(db_conn: psycopg.Connection) -> int:
    """Create a claimed row that died before producing any AI output."""
    tid = spawn_agent()
    with db_conn.cursor() as cur:
        cur.execute(
            "UPDATE agents_meta SET status = 'running', pid = %s, last_message_text = NULL "
            "WHERE id = %s",
            (_DEAD_PID, tid),
        )
    db_conn.commit()
    return tid


def test_reaps_dead_boot_phase_row_with_dead_pid(
    db_conn: psycopg.Connection, sync_pool: ConnectionPool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pre-message running row with a dead pid enters crash-resurrect backoff."""
    _stub_probe(monkeypatch, AgentProcessIdentity.GONE)
    tid = _make_dead_boot_phase_agent(db_conn)

    reaped = rd._reap_local_dead_boot_phase_agents(sync_pool, machine_name())

    assert reaped == [tid]
    with db_conn.cursor() as cur:
        cur.execute("SELECT status FROM agents_meta WHERE id = %s", (tid,))
        assert cur.fetchone()[0] == "terminated"  # type: ignore[index]


def _open_page(db_conn: psycopg.Connection, aid: int, name: str = "report") -> None:
    """Seed one open show() row, which terminate cascades may close (audit B2)."""
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO agent_pages (agent_id, name, port, host, title) "
            "VALUES (%s, %s, 18001, '127.0.0.1', 'Report')",
            (aid, name),
        )
    db_conn.commit()


def test_reaping_dead_boot_phase_row_publishes_page_closed(
    db_conn: psycopg.Connection, sync_pool: ConnectionPool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A reaped boot-phase row holding a show() page emits one PageClosed.

    The cascade closes this agent-owned row, so the event clears the frontend
    popover instead of leaving a stale entry until the next refresh.
    """
    _stub_probe(monkeypatch, AgentProcessIdentity.GONE)
    tid = _make_dead_boot_phase_agent(db_conn)
    _open_page(db_conn, tid)

    closed: list[tuple[int, str]] = []
    monkeypatch.setattr(
        rd,
        "publish_page_closed_sync",
        lambda aid, name: closed.append((aid, name)),  # pyright: ignore[reportUnknownArgumentType]
    )

    reaped = rd._reap_local_dead_boot_phase_agents(sync_pool, machine_name())

    assert reaped == [tid]
    assert closed == [(tid, "report")]
    with db_conn.cursor() as cur:
        cur.execute("SELECT closed_at IS NOT NULL FROM agent_pages WHERE agent_id = %s", (tid,))
        assert cur.fetchone()[0] is True  # type: ignore[index]


def test_leaves_boot_phase_row_with_live_pid(
    db_conn: psycopg.Connection, sync_pool: ConnectionPool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A live boot-phase process is left untouched."""
    _stub_probe(monkeypatch, AgentProcessIdentity.OWNED)
    tid = _make_dead_boot_phase_agent(db_conn)

    reaped = rd._reap_local_dead_boot_phase_agents(sync_pool, machine_name())

    assert reaped == []
    with db_conn.cursor() as cur:
        cur.execute("SELECT status FROM agents_meta WHERE id = %s", (tid,))
        assert cur.fetchone()[0] == "running"  # type: ignore[index]


def test_ignores_other_machines_rows(
    db_conn: psycopg.Connection, sync_pool: ConnectionPool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A row homed on another machine is never probed/reaped — its pid lives in
    a different host's pid space and is not ours to judge."""
    _stub_probe(monkeypatch, AgentProcessIdentity.GONE)
    tid = _make_dead_boot_phase_agent(db_conn)
    with db_conn.cursor() as cur:
        cur.execute("UPDATE agents_meta SET machine = 'other-host' WHERE id = %s", (tid,))
    db_conn.commit()

    reaped = rd._reap_local_dead_boot_phase_agents(sync_pool, machine_name())

    assert reaped == []
    with db_conn.cursor() as cur:
        cur.execute("SELECT status FROM agents_meta WHERE id = %s", (tid,))
        assert cur.fetchone()[0] == "running"  # type: ignore[index]


def test_leaves_boot_phase_scan_row_with_null_pid(
    db_conn: psycopg.Connection, sync_pool: ConnectionPool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A running row with no pid (invariant violation — claim always writes
    one) is left for manual attention, not guessed dead. The pid-pinned UPDATE
    could not safely match it anyway."""
    _stub_probe(monkeypatch, AgentProcessIdentity.GONE)
    tid = spawn_agent()
    with db_conn.cursor() as cur:
        cur.execute("UPDATE agents_meta SET status = 'running', pid = NULL WHERE id = %s", (tid,))
    db_conn.commit()

    reaped = rd._reap_local_dead_boot_phase_agents(sync_pool, machine_name())

    assert reaped == []
    with db_conn.cursor() as cur:
        cur.execute("SELECT status FROM agents_meta WHERE id = %s", (tid,))
        assert cur.fetchone()[0] == "running"  # type: ignore[index]


def test_boot_phase_scan_leaves_post_message_rows(
    db_conn: psycopg.Connection, sync_pool: ConnectionPool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A row with an AI message remains on the ordinary revive path."""
    _stub_probe(monkeypatch, AgentProcessIdentity.GONE)
    tid = spawn_agent()
    with db_conn.cursor() as cur:
        cur.execute(
            "UPDATE agents_meta SET status = 'running', pid = %s, last_message_text = 'done' "
            "WHERE id = %s",
            (_DEAD_PID, tid),
        )
    db_conn.commit()

    reaped = rd._reap_local_dead_boot_phase_agents(sync_pool, machine_name())

    assert reaped == []
    with db_conn.cursor() as cur:
        cur.execute("SELECT status FROM agents_meta WHERE id = %s", (tid,))
        assert cur.fetchone()[0] == "running"  # type: ignore[index]


# ────────────────────────────────────────────────────────────────────────
# _reap_local_unclaimed_idling — age-based sweep of unclaimed idling rows
# ────────────────────────────────────────────────────────────────────────
# Unclaimed idling rows carry no pid, so liveness is judged by age.

_GRACE_S = 60.0


def _make_unclaimed_idling_aged(
    db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch, *, age_s: float
) -> int:
    """Spawn an unclaimed idling row and backdate its status_changed_at by age_s.

    The backdating UPDATE touches only status_changed_at, not status, so the
    BEFORE-UPDATE-OF-status trigger does not fire and the planted value sticks.
    """
    tid = spawn_agent()
    with db_conn.cursor() as cur:
        cur.execute(
            "UPDATE agents_meta SET status_changed_at = now() - make_interval(secs => %s) "
            "WHERE id = %s",
            (age_s, tid),
        )
    db_conn.commit()
    return tid


def test_reaps_unclaimed_idling_row_older_than_grace(
    db_conn: psycopg.Connection, sync_pool: ConnectionPool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The dead-birth predicate is idling + NULL pid + age past grace."""
    tid = _make_unclaimed_idling_aged(db_conn, monkeypatch, age_s=2 * _GRACE_S)

    reaped = rd._reap_local_unclaimed_idling(sync_pool, machine_name(), _GRACE_S)

    assert reaped == [tid]
    with db_conn.cursor() as cur:
        cur.execute("SELECT status FROM agents_meta WHERE id = %s", (tid,))
        assert cur.fetchone()[0] == "terminated"  # type: ignore[index]


def test_reaping_unclaimed_idling_publishes_page_closed(
    db_conn: psycopg.Connection, sync_pool: ConnectionPool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A reaped stale unclaimed row holding open pages emits one PageClosed per
    page (audit B2) — an unclaimed row can hold pages reopened by resurrect's
    cascade_open, and the frontend popover must drop them on the reap."""
    tid = _make_unclaimed_idling_aged(db_conn, monkeypatch, age_s=2 * _GRACE_S)
    _open_page(db_conn, tid, name="panel")

    closed: list[tuple[int, str]] = []
    monkeypatch.setattr(
        rd,
        "publish_page_closed_sync",
        lambda aid, name: closed.append((aid, name)),  # pyright: ignore[reportUnknownArgumentType]
    )

    reaped = rd._reap_local_unclaimed_idling(sync_pool, machine_name(), _GRACE_S)

    assert reaped == [tid]
    assert closed == [(tid, "panel")]
    with db_conn.cursor() as cur:
        cur.execute("SELECT closed_at IS NOT NULL FROM agent_pages WHERE agent_id = %s", (tid,))
        assert cur.fetchone()[0] is True  # type: ignore[index]


def test_leaves_recent_unclaimed_idling_row(
    db_conn: psycopg.Connection, sync_pool: ConnectionPool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A freshly unclaimed idling row (within grace) is a normal mid-launch state —
    left alone so a merely-slow boot is never reaped."""
    tid = _make_unclaimed_idling_aged(db_conn, monkeypatch, age_s=1.0)

    reaped = rd._reap_local_unclaimed_idling(sync_pool, machine_name(), _GRACE_S)

    assert reaped == []
    with db_conn.cursor() as cur:
        cur.execute("SELECT status FROM agents_meta WHERE id = %s", (tid,))
        assert cur.fetchone()[0] == "idling"  # type: ignore[index]


def test_dead_birth_reaper_ignores_other_machines(
    db_conn: psycopg.Connection, sync_pool: ConnectionPool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stale unclaimed row homed elsewhere belongs to that host's reaper."""
    tid = _make_unclaimed_idling_aged(db_conn, monkeypatch, age_s=2 * _GRACE_S)
    with db_conn.cursor() as cur:
        cur.execute("UPDATE agents_meta SET machine = 'other-host' WHERE id = %s", (tid,))
    db_conn.commit()

    reaped = rd._reap_local_unclaimed_idling(sync_pool, machine_name(), _GRACE_S)

    assert reaped == []
    with db_conn.cursor() as cur:
        cur.execute("SELECT status FROM agents_meta WHERE id = %s", (tid,))
        assert cur.fetchone()[0] == "idling"  # type: ignore[index]


def test_dead_birth_reaper_ignores_claimed_idling_row(
    db_conn: psycopg.Connection, sync_pool: ConnectionPool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The dead-birth sweep must not reap an idling row that has a pid."""
    tid = _make_unclaimed_idling_aged(db_conn, monkeypatch, age_s=2 * _GRACE_S)
    with db_conn.cursor() as cur:
        cur.execute(
            "UPDATE agents_meta SET status = 'idling', pid = %s WHERE id = %s",
            (_DEAD_PID, tid),
        )
    db_conn.commit()

    reaped = rd._reap_local_unclaimed_idling(sync_pool, machine_name(), _GRACE_S)

    assert reaped == []
    with db_conn.cursor() as cur:
        cur.execute("SELECT status FROM agents_meta WHERE id = %s", (tid,))
        assert cur.fetchone()[0] == "idling"  # type: ignore[index]


# ────────────────────────────────────────────────────────────────────────
# status_changed_at trigger — the clock the dead-birth reaper reads
# ────────────────────────────────────────────────────────────────────────


def test_status_change_bumps_status_changed_at(
    db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A real status transition re-stamps status_changed_at."""
    tid = spawn_agent()
    with db_conn.cursor() as cur:
        cur.execute(
            "UPDATE agents_meta SET status_changed_at = now() - make_interval(secs => 999) "
            "WHERE id = %s",
            (tid,),
        )
        db_conn.commit()
        cur.execute("UPDATE agents_meta SET status = 'running' WHERE id = %s", (tid,))
        db_conn.commit()
        cur.execute(
            "SELECT now() - status_changed_at < make_interval(secs => 5) "
            "FROM agents_meta WHERE id = %s",
            (tid,),
        )
        assert cur.fetchone()[0] is True  # type: ignore[index]


def test_non_status_update_does_not_bump_status_changed_at(
    db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pid-only / index-only UPDATE must NOT bump status_changed_at — otherwise
    routine writes would keep resetting the dead-birth reaper's clock."""
    tid = spawn_agent()
    with db_conn.cursor() as cur:
        cur.execute(
            "UPDATE agents_meta SET status_changed_at = now() - make_interval(secs => 999) "
            "WHERE id = %s",
            (tid,),
        )
        db_conn.commit()
        cur.execute("UPDATE agents_meta SET pid = 4242 WHERE id = %s", (tid,))
        db_conn.commit()
        cur.execute(
            "SELECT now() - status_changed_at > make_interval(secs => 900) "
            "FROM agents_meta WHERE id = %s",
            (tid,),
        )
        assert cur.fetchone()[0] is True  # type: ignore[index]


# ────────────────────────────────────────────────────────────────────────
# _revive_local_dead_running_idling — revive pass for silently-dead live agents
# ────────────────────────────────────────────────────────────────────────
# A claimed 'running'/'idling' row owns a process. When it dies silently
# (OOM / SIGKILL / crash), revive clears its ownership columns, returns it to
# unclaimed 'idling', and launches a replacement. The safety floor: a normal
# idle agent has a LIVE pid -> never reaped.


def _make_live_status(db_conn: psycopg.Connection, *, status: str, pid: int) -> int:
    """Spawn an agent row and force it to a live-process status with a fixed pid."""
    tid = spawn_agent()
    with db_conn.cursor() as cur:
        cur.execute(
            "UPDATE agents_meta SET status = %s, pid = %s, last_message_text = 'done' "
            "WHERE id = %s",
            (status, pid, tid),
        )
    db_conn.commit()
    return tid


@pytest.mark.parametrize("status", ["running", "idling"])
def test_revives_live_status_row_with_dead_pid(
    db_conn: psycopg.Connection,
    sync_pool: ConnectionPool,
    monkeypatch: pytest.MonkeyPatch,
    status: str,
    launched_agents: list,
) -> None:
    """G5: 'running'/'idling' + dead pid -> relaunched in place (CAS to
    'idling' + launch) instead of reaped to 'terminated' — a rebooted
    machine's fleet comes back by itself."""
    launched_agents.clear()  # drop the spawn-setup launch; track only the revive
    _stub_probe(monkeypatch, AgentProcessIdentity.GONE)
    tid = _make_live_status(db_conn, status=status, pid=_DEAD_PID)

    revived = rd._revive_local_dead_running_idling(sync_pool, machine_name(), max_revive=50)

    assert revived == [tid]
    with db_conn.cursor() as cur:
        cur.execute("SELECT status, pid FROM agents_meta WHERE id = %s", (tid,))
        assert cur.fetchone() == ("idling", None)  # type: ignore[index]  # launch stubbed, no claim yet
    assert any(
        c.agent_id == tid  # pyright: ignore[reportUnknownArgumentType, reportUnknownMemberType]
        for c in launched_agents
    )  # at least one launch attempt (internal retries may record more)


@pytest.mark.parametrize("status", ["running", "idling"])
def test_leaves_live_status_row_with_live_pid(
    db_conn: psycopg.Connection,
    sync_pool: ConnectionPool,
    monkeypatch: pytest.MonkeyPatch,
    status: str,
) -> None:
    """The safety floor: a normal idle/running agent's pid still resolves to its
    own process -> left untouched. Mistaking a parked idle agent for a corpse
    would kill live work."""
    _stub_probe(monkeypatch, AgentProcessIdentity.OWNED)
    tid = _make_live_status(db_conn, status=status, pid=_DEAD_PID)

    revived = rd._revive_local_dead_running_idling(sync_pool, machine_name(), max_revive=50)

    assert revived == []
    with db_conn.cursor() as cur:
        cur.execute("SELECT status FROM agents_meta WHERE id = %s", (tid,))
        assert cur.fetchone()[0] == status  # type: ignore[index]


@pytest.mark.parametrize("status", ["running", "idling"])
def test_reaps_live_status_row_whose_pid_was_recycled(
    db_conn: psycopg.Connection,
    sync_pool: ConnectionPool,
    monkeypatch: pytest.MonkeyPatch,
    status: str,
) -> None:
    """Issue #1123's stranded row: the agent died, the OS reissued its pid to an
    unrelated process, and so the pid is ALIVE. Liveness passed that forever and
    the row sat 'idling' behind a dead agent across every reaper pass. Identity
    reaps it."""
    _stub_probe(monkeypatch, AgentProcessIdentity.FOREIGN)
    tid = _make_live_status(db_conn, status=status, pid=_DEAD_PID)

    revived = rd._revive_local_dead_running_idling(sync_pool, machine_name(), max_revive=50)

    assert revived == [tid]
    with db_conn.cursor() as cur:
        cur.execute("SELECT status, pid FROM agents_meta WHERE id = %s", (tid,))
        assert cur.fetchone() == ("idling", None)  # type: ignore[index]  # revived, pid cleared


@pytest.mark.parametrize("status", ["running", "idling"])
def test_leaves_live_status_row_whose_argv_is_unreadable(
    db_conn: psycopg.Connection,
    sync_pool: ConnectionPool,
    monkeypatch: pytest.MonkeyPatch,
    status: str,
) -> None:
    """UNREADABLE is not FOREIGN. A probe that could not read the argv (another
    user's process, a zombie) has no evidence the agent is gone, and this reaper
    is the one thing that can force a live agent's row to 'terminated' — so it
    stays its hand."""
    _stub_probe(monkeypatch, AgentProcessIdentity.UNREADABLE)
    tid = _make_live_status(db_conn, status=status, pid=_DEAD_PID)

    revived = rd._revive_local_dead_running_idling(sync_pool, machine_name(), max_revive=50)

    assert revived == []
    with db_conn.cursor() as cur:
        cur.execute("SELECT status FROM agents_meta WHERE id = %s", (tid,))
        assert cur.fetchone()[0] == status  # type: ignore[index]


def test_running_reaper_treats_permission_error_pid_as_alive(
    db_conn: psycopg.Connection, sync_pool: ConnectionPool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pid recycled to another user raises PermissionError on probe; the real
    process_alive counts that as alive, so the row is NOT reaped — we never
    declare a corpse when the pid maps to *some* running process."""

    def _kill_raises_permission(_pid: int, _sig: int) -> None:
        raise PermissionError

    monkeypatch.setattr(os, "kill", _kill_raises_permission)
    tid = _make_live_status(db_conn, status="running", pid=_DEAD_PID)

    reaped = rd._revive_local_dead_running_idling(sync_pool, machine_name(), max_revive=50)

    assert reaped == []
    with db_conn.cursor() as cur:
        cur.execute("SELECT status FROM agents_meta WHERE id = %s", (tid,))
        assert cur.fetchone()[0] == "running"  # type: ignore[index]


def test_running_reaper_ignores_other_machines(
    db_conn: psycopg.Connection, sync_pool: ConnectionPool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A 'running' row homed on another machine is never probed — its pid lives
    in a different host's pid space and is not ours to judge."""
    _stub_probe(monkeypatch, AgentProcessIdentity.GONE)
    tid = _make_live_status(db_conn, status="running", pid=_DEAD_PID)
    with db_conn.cursor() as cur:
        cur.execute("UPDATE agents_meta SET machine = 'other-host' WHERE id = %s", (tid,))
    db_conn.commit()

    reaped = rd._revive_local_dead_running_idling(sync_pool, machine_name(), max_revive=50)

    assert reaped == []
    with db_conn.cursor() as cur:
        cur.execute("SELECT status FROM agents_meta WHERE id = %s", (tid,))
        assert cur.fetchone()[0] == "running"  # type: ignore[index]


def test_running_reaper_leaves_null_pid(
    db_conn: psycopg.Connection, sync_pool: ConnectionPool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A 'running' row with no pid (invariant violation — a live agent always
    carries one) is left for manual attention, not guessed dead. The pid-pinned
    UPDATE could not safely match it anyway."""
    _stub_probe(monkeypatch, AgentProcessIdentity.GONE)
    tid = spawn_agent()
    with db_conn.cursor() as cur:
        cur.execute("UPDATE agents_meta SET status = 'running', pid = NULL WHERE id = %s", (tid,))
    db_conn.commit()

    reaped = rd._revive_local_dead_running_idling(sync_pool, machine_name(), max_revive=50)

    assert reaped == []
    with db_conn.cursor() as cur:
        cur.execute("SELECT status FROM agents_meta WHERE id = %s", (tid,))
        assert cur.fetchone()[0] == "running"  # type: ignore[index]


def test_running_reaper_aba_skips_status_or_pid_changed_row(
    db_conn: psycopg.Connection, sync_pool: ConnectionPool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ABA guard: between the SELECT (which saw the dead pid) and the UPDATE the
    row cycled to a fresh, live pid. The pid-pinned UPDATE (AND pid = <observed>)
    must not match it, so the now-live row is left untouched. Simulated by
    flipping the pid before the reaper's UPDATE fires, via an identity-probe stub
    that mutates the row on the probe call."""
    fresh_pid = _DEAD_PID + 1

    def _gone_then_mutate(_pid: int, _agent_id: int) -> AgentProcessIdentity:
        # First (and only) probe: report the process gone, but meanwhile the row
        # gets a fresh live pid — the reaper's pid-pinned UPDATE should miss it.
        with db_conn.cursor() as cur:
            cur.execute(
                "UPDATE agents_meta SET pid = %s WHERE id = %s", (fresh_pid, _captured["tid"])
            )
        db_conn.commit()
        return AgentProcessIdentity.GONE

    _captured: dict[str, int] = {}
    tid = _make_live_status(db_conn, status="running", pid=_DEAD_PID)
    _captured["tid"] = tid
    monkeypatch.setattr(rd, "probe_agent_process", _gone_then_mutate)

    reaped = rd._revive_local_dead_running_idling(sync_pool, machine_name(), max_revive=50)

    assert reaped == []
    with db_conn.cursor() as cur:
        cur.execute("SELECT status, pid FROM agents_meta WHERE id = %s", (tid,))
        row = cur.fetchone()
        assert row[0] == "running"  # type: ignore[index]
        assert row[1] == fresh_pid  # type: ignore[index]


def test_revive_cap_defers_remainder(
    db_conn: psycopg.Connection,
    sync_pool: ConnectionPool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AVA_REVIVE_MAX_PER_PASS is the anti-storm guard: with two dead rows and a
    cap of one, exactly one is revived this pass; the other drains on a later
    pass (the reaper cadence) instead of a 2-process launch in one tick."""
    _stub_probe(monkeypatch, AgentProcessIdentity.GONE)
    t1 = _make_live_status(db_conn, status="running", pid=_DEAD_PID)
    t2 = _make_live_status(db_conn, status="idling", pid=_DEAD_PID)

    revived = rd._revive_local_dead_running_idling(sync_pool, machine_name(), max_revive=1)

    assert len(revived) == 1
    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT status, pid FROM agents_meta WHERE id IN (%s, %s) ORDER BY id", (t1, t2)
        )
        rows = cur.fetchall()
    assert rows == [("idling", None), ("idling", _DEAD_PID)]


# ── revive_agent (G5: dead-pid row relaunched in place, no lifecycle inbound) ──


# ─── R1: lease-expired zombies (Task #1021) ─────────────────────────────────


def _make_lease_zombie(
    db_conn: psycopg.Connection, *, status: str, pid: int, lease: str | None = "expired"
) -> int:
    """Spawn a live-status row with `pid` and a lease that is expired (default),
    NULL (never granted — pre-lease code), or 'live'."""
    from datetime import UTC, datetime, timedelta

    tid = spawn_agent()
    lease_value = {
        "expired": datetime.now(UTC) - timedelta(seconds=1),
        "live": datetime.now(UTC) + timedelta(seconds=600),
        None: None,
    }[lease]
    with db_conn.cursor() as cur:
        cur.execute(
            "UPDATE agents_meta SET status = %s, pid = %s, lease_expires_at = %s WHERE id = %s",
            (status, pid, lease_value, tid),
        )
    db_conn.commit()
    return tid


@pytest.mark.parametrize("status", ["running", "idling"])
def test_collects_live_status_row_with_expired_lease(
    db_conn: psycopg.Connection,
    sync_pool: ConnectionPool,
    monkeypatch: pytest.MonkeyPatch,
    status: str,
) -> None:
    """R1 (Task #1021): a running/idling row whose lease expired is a zombie —
    the lease is the liveness authority, so even a resident pid gets killed; the
    revive pass then relaunches the row in place."""
    _stub_probe(monkeypatch, AgentProcessIdentity.OWNED)  # pid IS the agent
    killed: list[int] = []
    monkeypatch.setattr(rd, "force_kill", killed.append)
    tid = _make_lease_zombie(db_conn, status=status, pid=4242)

    zombies = rd._collect_local_lease_zombies(sync_pool, machine_name())

    assert zombies == [tid]
    assert killed == [4242]


@pytest.mark.parametrize("status", ["running", "idling"])
def test_collects_live_status_row_with_no_lease(
    db_conn: psycopg.Connection,
    sync_pool: ConnectionPool,
    monkeypatch: pytest.MonkeyPatch,
    status: str,
) -> None:
    """A NULL lease (pre-lease code claiming after the migration) is equally a
    zombie — the row is collected so the agent lands on code that renews."""
    _stub_probe(monkeypatch, AgentProcessIdentity.OWNED)
    killed: list[int] = []
    monkeypatch.setattr(rd, "force_kill", killed.append)
    tid = _make_lease_zombie(db_conn, status=status, pid=4242, lease=None)

    zombies = rd._collect_local_lease_zombies(sync_pool, machine_name())

    assert zombies == [tid]
    assert killed == [4242]


def test_collector_leaves_lease_live_rows_alone(
    db_conn: psycopg.Connection,
    sync_pool: ConnectionPool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A live lease is the alive reading — nothing to collect."""
    _stub_probe(monkeypatch, AgentProcessIdentity.OWNED)
    killed: list[int] = []
    monkeypatch.setattr(rd, "force_kill", killed.append)
    tid = _make_lease_zombie(db_conn, status="running", pid=4242, lease="live")

    assert rd._collect_local_lease_zombies(sync_pool, machine_name()) == []
    assert killed == []
    with db_conn.cursor() as cur:
        cur.execute("SELECT status FROM agents_meta WHERE id = %s", (tid,))
        assert cur.fetchone() == ("running",)  # type: ignore[index]


def test_collector_ignores_other_statuses(
    db_conn: psycopg.Connection,
    sync_pool: ConnectionPool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """'terminated' is outside the zombie scan entirely."""
    _stub_probe(monkeypatch, AgentProcessIdentity.OWNED)
    terminated = _make_lease_zombie(db_conn, status="terminated", pid=4243, lease=None)

    assert rd._collect_local_lease_zombies(sync_pool, machine_name()) == []
    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT status FROM agents_meta WHERE id = %s",
            (terminated,),
        )
        assert cur.fetchall() == [("terminated",)]


@pytest.mark.parametrize("change", ["renewed", "replacement", "terminated", "moved"])
def test_lease_reaper_rechecks_candidate_under_row_lock(
    db_conn: psycopg.Connection,
    sync_pool: ConnectionPool,
    monkeypatch: pytest.MonkeyPatch,
    change: str,
) -> None:
    """A stale candidate cannot license a kill after ownership/liveness changed."""
    tid = _make_lease_zombie(db_conn, status="running", pid=4242)
    killed: list[int] = []
    changed = False

    def probe(_pid: int, _agent_id: int) -> AgentProcessIdentity:
        nonlocal changed
        if not changed:
            changed = True
            updates: dict[str, LiteralString] = {
                "renewed": "UPDATE agents_meta SET lease_expires_at = now() + interval '10 minutes' WHERE id = %s",
                "replacement": "UPDATE agents_meta SET started_at = now() WHERE id = %s",
                "terminated": "UPDATE agents_meta SET status = 'terminated', termination_source = 'user' WHERE id = %s",
                "moved": "UPDATE agents_meta SET machine = 'replacement-host' WHERE id = %s",
            }
            with db_conn.cursor() as cur:
                cur.execute(updates[change], (tid,))
            db_conn.commit()
        return AgentProcessIdentity.OWNED

    monkeypatch.setattr(rd, "probe_agent_process", probe)
    monkeypatch.setattr(rd, "force_kill", killed.append)
    assert rd._collect_local_lease_zombies(sync_pool, machine_name()) == []
    assert killed == []


@pytest.mark.parametrize("unreadable_on", [1, 2])
def test_lease_reaper_never_signals_unreadable_identity(
    db_conn: psycopg.Connection,
    sync_pool: ConnectionPool,
    monkeypatch: pytest.MonkeyPatch,
    unreadable_on: int,
) -> None:
    _make_lease_zombie(db_conn, status="running", pid=4242)
    killed: list[int] = []
    probes = 0

    def probe(_pid: int, _agent_id: int) -> AgentProcessIdentity:
        nonlocal probes
        probes += 1
        return (
            AgentProcessIdentity.UNREADABLE
            if probes == unreadable_on
            else AgentProcessIdentity.OWNED
        )

    monkeypatch.setattr(rd, "probe_agent_process", probe)
    monkeypatch.setattr(rd, "force_kill", killed.append)
    assert rd._collect_local_lease_zombies(sync_pool, machine_name()) == []
    assert killed == []


class TestReviveAgent:
    def test_running_row_flips_to_idling_clears_pid_and_launches(
        self,
        db_conn: psycopg.Connection,
        launched_agents: list,
    ) -> None:
        """A 'running' row behind a dead pid -> CAS to unclaimed idling (pid cleared)
        + one launch; the checkpoint survives, so the revived agent resumes."""
        from ops.agent_wake import revive_agent

        tid = _make_live_status(db_conn, status="running", pid=_DEAD_PID)
        launched_agents.clear()  # drop the spawn-setup launch; track only the revive

        assert revive_agent(tid, _DEAD_PID) is True

        with db_conn.cursor() as cur:
            cur.execute("SELECT status, pid FROM agents_meta WHERE id = %s", (tid,))
            assert cur.fetchone() == ("idling", None)  # type: ignore[index]
        assert any(c.agent_id == tid for c in launched_agents)  # pyright: ignore[reportUnknownArgumentType, reportUnknownMemberType]

    def test_idling_row_flips_to_unclaimed_idling(
        self,
        db_conn: psycopg.Connection,
        launched_agents: list,
    ) -> None:
        from ops.agent_wake import revive_agent

        tid = _make_live_status(db_conn, status="idling", pid=_DEAD_PID)
        launched_agents.clear()

        assert revive_agent(tid, _DEAD_PID) is True

        with db_conn.cursor() as cur:
            cur.execute("SELECT status FROM agents_meta WHERE id = %s", (tid,))
            assert cur.fetchone()[0] == "idling"  # type: ignore[index]

    def test_inserts_no_lifecycle_inbound(
        self,
        db_conn: psycopg.Connection,
        launched_agents: list,
    ) -> None:
        """Revival is invisible to the agent — no resurrect / restart_completed
        inbound (Task #689 G5)."""
        from ops.agent_wake import revive_agent

        tid = _make_live_status(db_conn, status="running", pid=_DEAD_PID)
        launched_agents.clear()

        revive_agent(tid, _DEAD_PID)

        with db_conn.cursor() as cur:
            cur.execute("SELECT kind FROM inbound_messages WHERE agent_id = %s", (tid,))
            assert cur.fetchall() == []

    def test_wrong_pid_is_noop(self, db_conn: psycopg.Connection, launched_agents: list) -> None:
        """CAS re-asserts the probed pid: a row whose pid changed (already
        revived, or the process is actually alive) is never double-launched."""
        from ops.agent_wake import revive_agent

        tid = _make_live_status(db_conn, status="running", pid=_DEAD_PID)
        launched_agents.clear()

        assert revive_agent(tid, _DEAD_PID + 1) is False

        with db_conn.cursor() as cur:
            cur.execute("SELECT status, pid FROM agents_meta WHERE id = %s", (tid,))
            assert cur.fetchone() == ("running", _DEAD_PID)  # type: ignore[index]
        assert launched_agents == []

    def test_non_live_status_is_noop(
        self,
        db_conn: psycopg.Connection,
        launched_agents: list,
    ) -> None:
        """Only 'running'/'idling' rows are revivable — a 'terminated' row is
        crash-resurrect's business, not this pass's."""
        from ops.agent_wake import revive_agent

        tid = spawn_agent()
        with db_conn.cursor() as cur:
            cur.execute(
                "UPDATE agents_meta SET status = 'terminated', pid = %s WHERE id = %s",
                (_DEAD_PID, tid),
            )
        db_conn.commit()
        launched_agents.clear()

        assert revive_agent(tid, _DEAD_PID) is False

        with db_conn.cursor() as cur:
            cur.execute("SELECT status FROM agents_meta WHERE id = %s", (tid,))
            assert cur.fetchone()[0] == "terminated"  # type: ignore[index]
        assert launched_agents == []

    def test_double_revive_only_one_launches(
        self,
        db_conn: psycopg.Connection,
        launched_agents: list,
    ) -> None:
        """Idempotent under a double-trigger (two reap passes racing): the CAS
        picks a single winner; the second call sees an unclaimed idling row and no-ops."""
        from ops.agent_wake import revive_agent

        tid = _make_live_status(db_conn, status="idling", pid=_DEAD_PID)
        launched_agents.clear()

        first = revive_agent(tid, _DEAD_PID)
        second = revive_agent(tid, _DEAD_PID)  # row has no matching pid

        assert (first, second) == (True, False)
        with db_conn.cursor() as cur:
            cur.execute("SELECT status FROM agents_meta WHERE id = %s", (tid,))
            assert cur.fetchone()[0] == "idling"  # type: ignore[index]
