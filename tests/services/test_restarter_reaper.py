"""Restarter orphan-'starting' reaper unit tests.

A process flips its row 'allocated' -> 'starting' (writing its pid) early in
boot and only reaches 'running' once it enters the run loop. If it dies in
between (boot crash, OOM, SIGKILL, schema drift), the row strands at 'starting'
forever: the restart dispatch only touches 'restarting', and the gateway
zombie-reap is lazy. The restarter sweeps these — a 'starting' row whose pid is
no longer that agent's own process is forced to 'terminated'.

The reapers' predicate is process IDENTITY, not liveness (`ops.agent_identity`),
so it is the identity probe that is monkeypatched here rather than a pid check —
tests do not depend on real pids. The distinction has teeth: a recycled pid is
alive without being the agent, and reading that as "still running" is what left
prod rows stranded indefinitely (issue #1123).
"""

from __future__ import annotations

import os

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
    monkeypatch.setattr(rd, "probe_agent_process", lambda _pid, _agent_id: verdict)  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType]


@pytest.fixture
def sync_pool():
    pool = ConnectionPool(settings.data_plane.db_url, min_size=1, max_size=2, open=True)
    try:
        yield pool
    finally:
        pool.close()


def _make_starting(db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch) -> int:
    """Spawn an agent row and force it to 'starting' with a fixed pid."""
    tid = spawn_agent()
    with db_conn.cursor() as cur:
        cur.execute(
            "UPDATE agents_meta SET status = 'starting', pid = %s WHERE id = %s",
            (_DEAD_PID, tid),
        )
    db_conn.commit()
    return tid


def test_reaps_starting_row_with_dead_pid(
    db_conn: psycopg.Connection, sync_pool: ConnectionPool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """'starting' + dead pid -> forced to 'terminated', returned in the list."""
    _stub_probe(monkeypatch, AgentProcessIdentity.GONE)
    tid = _make_starting(db_conn, monkeypatch)

    reaped = rd._reap_local_dead_starting(sync_pool, machine_name())

    assert reaped == [tid]
    with db_conn.cursor() as cur:
        cur.execute("SELECT status FROM agents_meta WHERE id = %s", (tid,))
        assert cur.fetchone()[0] == "terminated"  # type: ignore[index]


def _open_page(db_conn: psycopg.Connection, aid: int, name: str = "report") -> None:
    """Seed one open agent_pages row (audit B2 tests)."""
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO agent_pages (agent_id, name, port, host, title) "
            "VALUES (%s, %s, 18001, '127.0.0.1', 'Report')",
            (aid, name),
        )
    db_conn.commit()


def test_reaping_starting_row_publishes_page_closed(
    db_conn: psycopg.Connection, sync_pool: ConnectionPool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A reaped 'starting' row holding open pages emits one PageClosed per page
    (audit B2) — the cascade trigger closes the page rows, the events clear the
    frontend popover instead of leaving stale entries until the next refresh."""
    _stub_probe(monkeypatch, AgentProcessIdentity.GONE)
    tid = _make_starting(db_conn, monkeypatch)
    _open_page(db_conn, tid)

    closed: list[tuple[int, str]] = []
    monkeypatch.setattr(
        rd,
        "publish_page_closed_sync",
        lambda aid, name: closed.append((aid, name)),  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType]
    )

    reaped = rd._reap_local_dead_starting(sync_pool, machine_name())

    assert reaped == [tid]
    assert closed == [(tid, "report")]
    with db_conn.cursor() as cur:
        cur.execute("SELECT closed_at IS NOT NULL FROM agent_pages WHERE agent_id = %s", (tid,))
        assert cur.fetchone()[0] is True  # type: ignore[index]


def test_leaves_starting_row_with_live_pid(
    db_conn: psycopg.Connection, sync_pool: ConnectionPool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """'starting' + live pid = still mid-boot -> left untouched."""
    _stub_probe(monkeypatch, AgentProcessIdentity.OWNED)
    tid = _make_starting(db_conn, monkeypatch)

    reaped = rd._reap_local_dead_starting(sync_pool, machine_name())

    assert reaped == []
    with db_conn.cursor() as cur:
        cur.execute("SELECT status FROM agents_meta WHERE id = %s", (tid,))
        assert cur.fetchone()[0] == "starting"  # type: ignore[index]


def test_ignores_other_machines_rows(
    db_conn: psycopg.Connection, sync_pool: ConnectionPool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A row homed on another machine is never probed/reaped — its pid lives in
    a different host's pid space and is not ours to judge."""
    _stub_probe(monkeypatch, AgentProcessIdentity.GONE)
    tid = _make_starting(db_conn, monkeypatch)
    with db_conn.cursor() as cur:
        cur.execute("UPDATE agents_meta SET machine = 'other-host' WHERE id = %s", (tid,))
    db_conn.commit()

    reaped = rd._reap_local_dead_starting(sync_pool, machine_name())

    assert reaped == []
    with db_conn.cursor() as cur:
        cur.execute("SELECT status FROM agents_meta WHERE id = %s", (tid,))
        assert cur.fetchone()[0] == "starting"  # type: ignore[index]


def test_leaves_starting_row_with_null_pid(
    db_conn: psycopg.Connection, sync_pool: ConnectionPool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A 'starting' row with no pid (invariant violation — claim always writes
    one) is left for manual attention, not guessed dead. The pid-pinned UPDATE
    could not safely match it anyway."""
    _stub_probe(monkeypatch, AgentProcessIdentity.GONE)
    tid = spawn_agent()
    with db_conn.cursor() as cur:
        cur.execute("UPDATE agents_meta SET status = 'starting', pid = NULL WHERE id = %s", (tid,))
    db_conn.commit()

    reaped = rd._reap_local_dead_starting(sync_pool, machine_name())

    assert reaped == []
    with db_conn.cursor() as cur:
        cur.execute("SELECT status FROM agents_meta WHERE id = %s", (tid,))
        assert cur.fetchone()[0] == "starting"  # type: ignore[index]


def test_leaves_non_starting_rows(
    db_conn: psycopg.Connection, sync_pool: ConnectionPool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The 'starting' sweep only touches 'starting' — a 'running' row (even with
    a dead pid) is the running/idling reaper's job, not this one's."""
    _stub_probe(monkeypatch, AgentProcessIdentity.GONE)
    tid = spawn_agent()
    with db_conn.cursor() as cur:
        cur.execute(
            "UPDATE agents_meta SET status = 'running', pid = %s WHERE id = %s",
            (_DEAD_PID, tid),
        )
    db_conn.commit()

    reaped = rd._reap_local_dead_starting(sync_pool, machine_name())

    assert reaped == []
    with db_conn.cursor() as cur:
        cur.execute("SELECT status FROM agents_meta WHERE id = %s", (tid,))
        assert cur.fetchone()[0] == "running"  # type: ignore[index]


# ────────────────────────────────────────────────────────────────────────
# _reap_local_stale_allocated — age-based sweep of 'allocated' orphans
# ────────────────────────────────────────────────────────────────────────
# 'allocated' carries no pid, so liveness is judged by age: status_changed_at
# (stamped by the DB trigger on entry into 'allocated') older than a grace.

_GRACE_S = 60.0


def _make_allocated_aged(
    db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch, *, age_s: float
) -> int:
    """Spawn an 'allocated' row and backdate its status_changed_at by age_s.

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


def test_reaps_allocated_row_older_than_grace(
    db_conn: psycopg.Connection, sync_pool: ConnectionPool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """'allocated' past the grace = the launched process never claimed -> reap."""
    tid = _make_allocated_aged(db_conn, monkeypatch, age_s=2 * _GRACE_S)

    reaped = rd._reap_local_stale_allocated(sync_pool, machine_name(), _GRACE_S)

    assert reaped == [tid]
    with db_conn.cursor() as cur:
        cur.execute("SELECT status FROM agents_meta WHERE id = %s", (tid,))
        assert cur.fetchone()[0] == "terminated"  # type: ignore[index]


def test_reaping_stale_allocated_publishes_page_closed(
    db_conn: psycopg.Connection, sync_pool: ConnectionPool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A reaped stale-'allocated' row holding open pages emits one PageClosed per
    page (audit B2) — an 'allocated' row can hold pages reopened by resurrect's
    cascade_open, and the frontend popover must drop them on the reap."""
    tid = _make_allocated_aged(db_conn, monkeypatch, age_s=2 * _GRACE_S)
    _open_page(db_conn, tid, name="panel")

    closed: list[tuple[int, str]] = []
    monkeypatch.setattr(
        rd,
        "publish_page_closed_sync",
        lambda aid, name: closed.append((aid, name)),  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType]
    )

    reaped = rd._reap_local_stale_allocated(sync_pool, machine_name(), _GRACE_S)

    assert reaped == [tid]
    assert closed == [(tid, "panel")]
    with db_conn.cursor() as cur:
        cur.execute("SELECT closed_at IS NOT NULL FROM agent_pages WHERE agent_id = %s", (tid,))
        assert cur.fetchone()[0] is True  # type: ignore[index]


def test_leaves_recent_allocated_row(
    db_conn: psycopg.Connection, sync_pool: ConnectionPool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A freshly 'allocated' row (within grace) is a normal mid-launch state —
    left alone so a merely-slow boot is never reaped."""
    tid = _make_allocated_aged(db_conn, monkeypatch, age_s=1.0)

    reaped = rd._reap_local_stale_allocated(sync_pool, machine_name(), _GRACE_S)

    assert reaped == []
    with db_conn.cursor() as cur:
        cur.execute("SELECT status FROM agents_meta WHERE id = %s", (tid,))
        assert cur.fetchone()[0] == "allocated"  # type: ignore[index]


def test_allocated_reaper_ignores_other_machines(
    db_conn: psycopg.Connection, sync_pool: ConnectionPool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stale 'allocated' row homed elsewhere belongs to that host's reaper."""
    tid = _make_allocated_aged(db_conn, monkeypatch, age_s=2 * _GRACE_S)
    with db_conn.cursor() as cur:
        cur.execute("UPDATE agents_meta SET machine = 'other-host' WHERE id = %s", (tid,))
    db_conn.commit()

    reaped = rd._reap_local_stale_allocated(sync_pool, machine_name(), _GRACE_S)

    assert reaped == []
    with db_conn.cursor() as cur:
        cur.execute("SELECT status FROM agents_meta WHERE id = %s", (tid,))
        assert cur.fetchone()[0] == "allocated"  # type: ignore[index]


def test_allocated_reaper_ignores_starting(
    db_conn: psycopg.Connection, sync_pool: ConnectionPool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The allocated sweep only touches 'allocated' — a long-lived 'starting'
    row is the dead-pid reaper's job, judged by pid not age."""
    tid = _make_allocated_aged(db_conn, monkeypatch, age_s=2 * _GRACE_S)
    with db_conn.cursor() as cur:
        cur.execute(
            "UPDATE agents_meta SET status = 'starting', pid = %s WHERE id = %s",
            (_DEAD_PID, tid),
        )
    db_conn.commit()

    reaped = rd._reap_local_stale_allocated(sync_pool, machine_name(), _GRACE_S)

    assert reaped == []
    with db_conn.cursor() as cur:
        cur.execute("SELECT status FROM agents_meta WHERE id = %s", (tid,))
        assert cur.fetchone()[0] == "starting"  # type: ignore[index]


# ────────────────────────────────────────────────────────────────────────
# status_changed_at trigger — the clock the allocated reaper reads
# ────────────────────────────────────────────────────────────────────────


def test_status_change_bumps_status_changed_at(
    db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A real status transition re-stamps status_changed_at (so resurrect's
    terminated -> allocated resets the reaper clock)."""
    tid = spawn_agent()
    with db_conn.cursor() as cur:
        cur.execute(
            "UPDATE agents_meta SET status_changed_at = now() - make_interval(secs => 999) "
            "WHERE id = %s",
            (tid,),
        )
        db_conn.commit()
        cur.execute("UPDATE agents_meta SET status = 'starting' WHERE id = %s", (tid,))
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
    routine writes would keep resetting the allocated reaper's clock."""
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
# 'running'/'idling' both mean "the process owning this row exists". When it
# dies silently (OOM / SIGKILL / crash) the row keeps its status and the agent
# masquerades as alive. A row whose pid is dead is forced to 'terminated'. The
# safety floor: a normal idle agent has a LIVE pid -> never reaped.


def _make_live_status(db_conn: psycopg.Connection, *, status: str, pid: int) -> int:
    """Spawn an agent row and force it to a live-process status with a fixed pid."""
    tid = spawn_agent()
    with db_conn.cursor() as cur:
        cur.execute(
            "UPDATE agents_meta SET status = %s, pid = %s WHERE id = %s",
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
    launched_agents: list,  # pyright: ignore[reportMissingTypeArgument, reportUnknownParameterType]
) -> None:
    """G5: 'running'/'idling' + dead pid -> relaunched in place (CAS to
    'allocated' + launch) instead of reaped to 'terminated' — a rebooted
    machine's fleet comes back by itself."""
    launched_agents.clear()  # drop the spawn-setup launch; track only the revive
    _stub_probe(monkeypatch, AgentProcessIdentity.GONE)
    tid = _make_live_status(db_conn, status=status, pid=_DEAD_PID)

    revived = rd._revive_local_dead_running_idling(sync_pool, machine_name(), max_revive=50)

    assert revived == [tid]
    with db_conn.cursor() as cur:
        cur.execute("SELECT status, pid FROM agents_meta WHERE id = %s", (tid,))
        assert cur.fetchone() == ("allocated", None)  # type: ignore[index]  # launch stubbed, no claim to 'starting'
    assert any(
        c.agent_id == tid  # pyright: ignore[reportUnknownArgumentType, reportUnknownMemberType]
        for c in launched_agents  # pyright: ignore[reportUnknownArgumentType, reportUnknownMemberType, reportUnknownVariableType]
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
    the row sat 'idling' behind a dead agent across every reaper pass while
    hibernation kept signalling the stranger. Identity reaps it."""
    _stub_probe(monkeypatch, AgentProcessIdentity.FOREIGN)
    tid = _make_live_status(db_conn, status=status, pid=_DEAD_PID)

    revived = rd._revive_local_dead_running_idling(sync_pool, machine_name(), max_revive=50)

    assert revived == [tid]
    with db_conn.cursor() as cur:
        cur.execute("SELECT status, pid FROM agents_meta WHERE id = %s", (tid,))
        assert cur.fetchone() == ("allocated", None)  # type: ignore[index]  # revived, pid cleared


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
        cur.execute("SELECT status FROM agents_meta WHERE id IN (%s, %s) ORDER BY id", (t1, t2))
        statuses = [r[0] for r in cur.fetchall()]
    assert statuses.count("allocated") == 1
    assert statuses.count("running") + statuses.count("idling") == 1


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


def test_collector_ignores_hibernating_and_other_statuses(
    db_conn: psycopg.Connection,
    sync_pool: ConnectionPool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """'hibernating' is reaper-exempt (swapped out, no renewal by design); the
    other statuses are outside the zombie scan entirely."""
    _stub_probe(monkeypatch, AgentProcessIdentity.OWNED)
    hibernating = _make_lease_zombie(db_conn, status="hibernating", pid=4242, lease=None)
    terminated = _make_lease_zombie(db_conn, status="terminated", pid=4243, lease=None)

    assert rd._collect_local_lease_zombies(sync_pool, machine_name()) == []
    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT status FROM agents_meta WHERE id IN (%s, %s) ORDER BY id",
            (hibernating, terminated),
        )
        assert cur.fetchall() == [("hibernating",), ("terminated",)]


class TestReviveAgent:
    def test_running_row_flips_to_allocated_clears_pid_and_launches(
        self,
        db_conn: psycopg.Connection,
        launched_agents: list,  # pyright: ignore[reportMissingTypeArgument, reportUnknownParameterType]
    ) -> None:
        """A 'running' row behind a dead pid -> CAS to 'allocated' (pid cleared)
        + one launch; the checkpoint survives, so the revived agent resumes."""
        from ops.agent_wake import revive_agent

        tid = _make_live_status(db_conn, status="running", pid=_DEAD_PID)
        launched_agents.clear()  # drop the spawn-setup launch; track only the revive

        assert revive_agent(tid, _DEAD_PID) is True

        with db_conn.cursor() as cur:
            cur.execute("SELECT status, pid FROM agents_meta WHERE id = %s", (tid,))
            assert cur.fetchone() == ("allocated", None)  # type: ignore[index]
        assert any(c.agent_id == tid for c in launched_agents)  # pyright: ignore[reportUnknownArgumentType, reportUnknownMemberType, reportUnknownVariableType]

    def test_idling_row_flips_to_allocated(
        self,
        db_conn: psycopg.Connection,
        launched_agents: list,  # pyright: ignore[reportMissingTypeArgument, reportUnknownParameterType]
    ) -> None:
        from ops.agent_wake import revive_agent

        tid = _make_live_status(db_conn, status="idling", pid=_DEAD_PID)
        launched_agents.clear()

        assert revive_agent(tid, _DEAD_PID) is True

        with db_conn.cursor() as cur:
            cur.execute("SELECT status FROM agents_meta WHERE id = %s", (tid,))
            assert cur.fetchone()[0] == "allocated"  # type: ignore[index]

    def test_inserts_no_lifecycle_inbound(
        self,
        db_conn: psycopg.Connection,
        launched_agents: list,  # pyright: ignore[reportMissingTypeArgument, reportUnknownParameterType]
    ) -> None:
        """Revival is invisible to the agent — no resurrect / restart_completed
        inbound, exactly like a hibernation swap-in (Task #689 G5)."""
        from ops.agent_wake import revive_agent

        tid = _make_live_status(db_conn, status="running", pid=_DEAD_PID)
        launched_agents.clear()

        revive_agent(tid, _DEAD_PID)

        with db_conn.cursor() as cur:
            cur.execute("SELECT kind FROM inbound_messages WHERE agent_id = %s", (tid,))
            assert cur.fetchall() == []

    def test_wrong_pid_is_noop(self, db_conn: psycopg.Connection, launched_agents: list) -> None:  # pyright: ignore[reportMissingTypeArgument, reportUnknownParameterType]
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
        launched_agents: list,  # pyright: ignore[reportMissingTypeArgument, reportUnknownParameterType]
    ) -> None:
        """Only 'running'/'idling' rows are revivable — a 'hibernating' row is
        the hibernation controller's (swap-in on inbound), not this pass's."""
        from ops.agent_wake import revive_agent

        tid = spawn_agent()
        with db_conn.cursor() as cur:
            cur.execute(
                "UPDATE agents_meta SET status = 'hibernating', pid = %s WHERE id = %s",
                (_DEAD_PID, tid),
            )
        db_conn.commit()
        launched_agents.clear()

        assert revive_agent(tid, _DEAD_PID) is False

        with db_conn.cursor() as cur:
            cur.execute("SELECT status FROM agents_meta WHERE id = %s", (tid,))
            assert cur.fetchone()[0] == "hibernating"  # type: ignore[index]
        assert launched_agents == []

    def test_double_revive_only_one_launches(
        self,
        db_conn: psycopg.Connection,
        launched_agents: list,  # pyright: ignore[reportMissingTypeArgument, reportUnknownParameterType]
    ) -> None:
        """Idempotent under a double-trigger (two reap passes racing): the CAS
        picks a single winner; the second call sees 'allocated' and no-ops."""
        from ops.agent_wake import revive_agent

        tid = _make_live_status(db_conn, status="idling", pid=_DEAD_PID)
        launched_agents.clear()

        first = revive_agent(tid, _DEAD_PID)
        second = revive_agent(tid, _DEAD_PID)  # row is now 'allocated'

        assert (first, second) == (True, False)
        with db_conn.cursor() as cur:
            cur.execute("SELECT status FROM agents_meta WHERE id = %s", (tid,))
            assert cur.fetchone()[0] == "allocated"  # type: ignore[index]
