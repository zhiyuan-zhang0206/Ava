"""`_quiesce_all_agents` + its placement in the gateway orchestration.

The quiesce step is the "stop-the-world" before the schema migration: signal
every live agent to restart, wait until none are running. Two test layers:

- real test DB (`db_conn`): seed agents_meta rows, run quiesce, assert it
  INSERTs one restart/source='system:update' inbound per live agent and only
  for running/idling agents; assert it returns once the rows flip to
  'restarting' (a background thread flips them mid-poll, mirroring how an agent
  process transitions itself).
- stubbed shared.db helpers: drive the timeout path without a real stuck agent
  (count never drops) — assert it logs stragglers to stderr and returns without
  raising. The agents_meta / inbound SQL itself is covered in tests/shared/test_db.py.
- call-order: monkeypatch the orchestration helpers, assert quiesce runs after
  Phase A and before the local update (the migrating step).
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

import psycopg
import pytest

from cli import commands as _cli
from cli.commands import update as _up
from shared.config import settings
from shared.db import create_agent


def _seed_agent(db_conn: psycopg.Connection, status: str, *, live_lease: bool = True) -> int:
    """Create an agent + its agents_meta row in the given status, return id.

    `live_lease` grants the R1 liveness lease (default True — a seeded live
    agent renews like a real one); pass False to seed a lease-less (pre-lease /
    zombie) row, which the alive predicate reads as dead."""
    from datetime import UTC, datetime, timedelta

    agent_id = create_agent(db_conn)
    lease = datetime.now(UTC) + timedelta(seconds=600) if live_lease else None
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO agents_meta (id, spawner, status, lease_expires_at) "
            "VALUES (%s, 'test', %s, %s) "
            "ON CONFLICT (id) DO UPDATE SET status = EXCLUDED.status, "
            "    lease_expires_at = EXCLUDED.lease_expires_at",
            (agent_id, status, lease),
        )
    db_conn.commit()
    return agent_id


def _inbound_rows(db_conn: psycopg.Connection, agent_id: int) -> list[tuple[str, str, str]]:
    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT kind, source, content FROM inbound_messages WHERE agent_id = %s",
            (agent_id,),
        )
        return cur.fetchall()


# ─── INSERT correctness (real test DB) ────────────────────────────────────────


def test_quiesce_signals_only_live_agents(db_conn: psycopg.Connection) -> None:
    """One restart/source='system:update' inbound per running/idling agent;
    terminated/restarting/allocated agents get none. After signalling, flip the
    live rows to 'restarting' so the poll terminates (mirrors the agent process
    taking itself down)."""
    running = _seed_agent(db_conn, "running")
    idling = _seed_agent(db_conn, "idling")
    terminated = _seed_agent(db_conn, "terminated")
    restarting = _seed_agent(db_conn, "restarting")
    allocated = _seed_agent(db_conn, "allocated")

    # Quiesce will poll until live count hits 0; flip the two live agents to
    # 'restarting' from a background thread, the way a real agent claim node
    # would. Flip only AFTER both restart inbounds exist — the rows mark that
    # quiesce finished its signalling phase. (A fixed pre-flip sleep raced that
    # phase under xdist load: flipping first made quiesce see zero live agents
    # and signal nobody.) If the marker never appears, flip at the deadline
    # anyway so quiesce's poll terminates and the test fails on the assertions
    # instead of riding out quiesce's own timeout.
    def _flip() -> None:
        deadline = time.monotonic() + 8.0
        with psycopg.connect(settings.data_plane.db_url) as conn:
            while time.monotonic() < deadline:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT count(*) FROM inbound_messages "
                        "WHERE agent_id = ANY(%s) AND kind = 'restart'",
                        ([running, idling],),
                    )
                    row = cur.fetchone()
                    assert row is not None  # count(*) always returns one row
                    signalled = row[0] == 2
                conn.commit()  # end the read tx so the next poll sees fresh rows
                if signalled:
                    break
                time.sleep(0.02)
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE agents_meta SET status = 'restarting' WHERE id IN (%s, %s)",
                    (running, idling),
                )
            conn.commit()

    t = threading.Thread(target=_flip)
    t.start()
    _up._quiesce_all_agents(timeout_s=10.0)
    t.join()

    assert _inbound_rows(db_conn, running) == [("restart", "system:update", "")]
    assert _inbound_rows(db_conn, idling) == [("restart", "system:update", "")]
    assert _inbound_rows(db_conn, terminated) == []
    assert _inbound_rows(db_conn, restarting) == []
    assert _inbound_rows(db_conn, allocated) == []


def test_quiesce_returns_immediately_when_no_live_agents(db_conn: psycopg.Connection) -> None:
    """No running/idling agents → no inbound inserted, returns at once (no poll
    loop, no timeout)."""
    terminated = _seed_agent(db_conn, "terminated")

    started = time.monotonic()
    _up._quiesce_all_agents(timeout_s=10.0)
    elapsed = time.monotonic() - started

    assert _inbound_rows(db_conn, terminated) == []
    assert elapsed < 1.0, "with zero live agents quiesce must not enter the poll sleep"


# ─── timeout path (stubbed live-agent helpers, no real stuck agent) ───────────


def test_quiesce_timeout_logs_stragglers_without_raising(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A live set that never drains must exhaust the deadline, log the
    straggler ids to stderr, and return (never raise — a hang is worse than a
    degraded straggler). Already-signalled live agents must NOT be re-signalled
    on later passes (a mid-turn agent consumes its restart at the turn
    boundary; piling on more restarts would bounce it once per extra inbound).

    The agents_meta / inbound_messages SQL lives in shared.db now; this drives
    the cli orchestration's timeout branch by stubbing those helpers (the live
    set never drains), keeping the test at the cli layer. SQL correctness is
    covered in tests/shared/test_db.py.
    """
    from shared import db as _db

    signal_calls: list[set[int]] = []

    def _signal(source: str, *, exclude_agent_ids=frozenset()):
        signal_calls.append(set(exclude_agent_ids))  # pyright: ignore[reportUnknownArgumentType]
        return [i for i in (7, 9) if i not in exclude_agent_ids]

    monkeypatch.setattr(_db, "signal_live_agents_restart", _signal)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_db, "list_live_agent_ids", lambda: [7, 9])
    monkeypatch.setattr(_up.time, "sleep", lambda _s: None)  # pyright: ignore[reportUnknownArgumentType]
    # Advance the clock a fraction of the timeout per call so the loop runs a
    # few passes, then crosses the deadline (robust to the exact call count).
    clock = {"t": 0.0}

    def _monotonic() -> float:
        clock["t"] += 0.3
        return clock["t"]

    monkeypatch.setattr(_up.time, "monotonic", _monotonic)

    _up._quiesce_all_agents(timeout_s=1.0)  # must not raise

    err = capsys.readouterr().err
    assert "timed out" in err
    assert "[7, 9]" in err
    # First call is the initial bulk signal (no exclusions); both stragglers
    # are then in the signalled set, so no later pass re-signals them.
    assert signal_calls[0] == set()
    assert all(c == {7, 9} for c in signal_calls[1:])


def test_quiesce_re_signals_agent_that_becomes_live_mid_quiesce(
    db_conn: psycopg.Connection,
) -> None:
    """An agent that becomes live only after the initial bulk signal (the
    `ava.self.update()` initiator respawned from its own `self:update` restart,
    or a spawn completing mid-quiesce) must be signalled by a later convergence
    pass — otherwise it rides out the rollout on old code (the 2026-07-13
    incident: the initiator was excluded from the bulk signal, respawned before
    the restarter pause landed, and stayed live on old code through the whole
    rollout).

    Seed one idling agent (baseline, signalled by the initial pass) and one
    'restarting' agent (the initiator mid-respawn — invisible to the initial
    pass). A background thread then flips the initiator to 'running' (respawn
    complete), waits for its re-signal inbound to appear, and finally flips
    both to 'restarting' so quiesce converges.
    """
    baseline = _seed_agent(db_conn, "idling")
    initiator = _seed_agent(db_conn, "restarting")

    def _drive() -> None:
        deadline = time.monotonic() + 8.0
        with psycopg.connect(settings.data_plane.db_url) as conn:

            def _restart_count(agent_id: int) -> int:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT count(*) FROM inbound_messages "
                        "WHERE agent_id = %s AND kind = 'restart'",
                        (agent_id,),
                    )
                    row = cur.fetchone()
                    assert row is not None
                    n: int = row[0]
                conn.commit()  # end the read tx so the next poll sees fresh rows
                return n

            # Wait for the initial bulk signal (baseline signalled), then
            # complete the initiator's respawn.
            while time.monotonic() < deadline and _restart_count(baseline) < 1:
                time.sleep(0.02)
            with conn.cursor() as cur:
                cur.execute("UPDATE agents_meta SET status = 'running' WHERE id = %s", (initiator,))
            conn.commit()
            # Wait for the convergence pass to re-signal the initiator.
            while time.monotonic() < deadline and _restart_count(initiator) < 1:
                time.sleep(0.02)
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE agents_meta SET status = 'restarting' WHERE id IN (%s, %s)",
                    (baseline, initiator),
                )
            conn.commit()

    t = threading.Thread(target=_drive)
    t.start()
    _up._quiesce_all_agents(timeout_s=10.0)
    t.join()

    assert _inbound_rows(db_conn, baseline) == [("restart", "system:update", "")]
    assert _inbound_rows(db_conn, initiator) == [("restart", "system:update", "")]


# ─── call order in the gateway orchestration ────────────────────────────


def test_orchestration_quiesces_after_phase_a_before_local_update(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`_run_gateway_orchestration` must call `_quiesce_all_agents`
    strictly between Phase A (local pause + cluster/stop fan-out) and the local
    update (which migrates) — no old-code agent may be live during the migration.
    The local restarter must be paused BEFORE the remote fan-out: the fan-out
    blocks up to its timeout per unreachable host, and any agent exiting in that
    window (the `ava.self.update()` initiator on the pre-wait SDK) would be
    respawned on old code by a still-running local restarter (2026-07-13)."""
    order: list[str] = []

    # backend change → full orchestration (avoid a real git fetch in the test)
    monkeypatch.setattr(_cli, "_changed_paths_vs_origin", lambda: ["gateway/app.py"])
    # This test asserts quiesce ordering, not the migration-layout vet; stub the vet
    # so it does not git-ls-tree the real origin/main (whose layout is orthogonal here).
    monkeypatch.setattr(_up, "_vet_rollout_target", lambda _sha: None)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_cli, "_list_agent_runners", lambda: [("wsl", "http://unused")])

    def _fan_out(_hosts, path, _timeout, payload=None):
        order.append(f"fan_out:{path}")
        return [("wsl", "ok", "")]

    monkeypatch.setattr(_cli, "_fan_out", _fan_out)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr("ops.cluster.pause_local_cluster", lambda: order.append("pause_local"))
    monkeypatch.setattr(_cli, "_quiesce_all_agents", lambda **_: order.append("quiesce"))  # pyright: ignore[reportUnknownArgumentType]

    def _local(_repo, **_kw):
        order.append("local_update")
        return 0

    monkeypatch.setattr(_cli, "_run_gateway_local_update", _local)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_cli, "_poll_until_unpaused", lambda _hosts: order.append("poll") or {})  # pyright: ignore[reportUnknownArgumentType]

    rc = _cli._run_gateway_orchestration(Path("/unused"), origin="test-origin")
    assert rc == 0

    a_idx = order.index("fan_out:/api/cluster/stop")
    p_idx = order.index("pause_local")
    q_idx = order.index("quiesce")
    l_idx = order.index("local_update")
    assert p_idx < a_idx < q_idx < l_idx, (
        f"pause_local must run before Phase A, quiesce after both; got {order}"
    )


def test_orchestration_quiesces_even_without_agent_runners(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Single-host path (no agent-runners registered) pauses the local restarter,
    then quiesces local agents before migrating — the local restarter must not
    respawn quiesced agents while the gateway is updating."""
    order: list[str] = []

    monkeypatch.setattr(_cli, "_changed_paths_vs_origin", lambda: ["gateway/app.py"])
    monkeypatch.setattr(
        _up,
        "_vet_rollout_target",
        lambda _sha: None,  # pyright: ignore[reportUnknownArgumentType]
    )  # ordering test, not the vet  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_cli, "_list_agent_runners", list)
    monkeypatch.setattr("ops.cluster.pause_local_cluster", lambda: order.append("pause_local"))
    monkeypatch.setattr(_cli, "_quiesce_all_agents", lambda **_: order.append("quiesce"))  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(
        _cli,
        "_run_gateway_local_update",
        lambda _repo, **_kw: order.append("local_update") or 0,  # pyright: ignore[reportUnknownArgumentType]
    )

    rc = _cli._run_gateway_orchestration(Path("/unused"), origin="test-origin")
    assert rc == 0
    assert order == ["pause_local", "quiesce", "local_update"]


def test_orchestration_aborts_when_update_lock_held(monkeypatch: pytest.MonkeyPatch) -> None:
    """A second gateway update aborts (rc=1) without running the inner
    orchestration when a live holder already owns the cluster update lock — the
    serialization that stops two rollouts advancing the schema at once
    (the 2026-06-01 collision)."""
    from shared.cluster_lock import acquire_update_lock, release_update_lock

    ran: list[bool] = []
    monkeypatch.setattr(
        _up,
        "_run_gateway_orchestration_inner",
        lambda *_a, **_kw: ran.append(True) or 0,  # pyright: ignore[reportUnknownArgumentType]
    )

    assert acquire_update_lock("other-holder") is True
    try:
        rc = _cli._run_gateway_orchestration(Path("/unused"), origin="test-origin")
        assert rc == 1
        assert ran == []  # inner orchestration never ran
    finally:
        release_update_lock("other-holder")


# ─── update modes: smooth / force ─────────────────────────────────────────────


def test_quiesce_timeout_smooth_is_exec_timeout_plus_20pct() -> None:
    """Smooth mode waits out the longest possible single execute_code, plus a 20%
    grace margin — a healthy agent's exec is guaranteed to end and reach its
    turn-boundary claim before the force-reap backstop fires."""
    expected = settings.sandbox.exec_timeout_seconds * 1.2
    assert _up._quiesce_timeout_s("smooth") == pytest.approx(expected)  # pyright: ignore[reportUnknownMemberType]


def test_quiesce_timeout_force_is_short() -> None:
    """Force mode waits only for idle agents to drain (~10s); long execs are cut
    short and their stragglers force-reaped."""
    assert _up._quiesce_timeout_s("force") == pytest.approx(10.0)  # pyright: ignore[reportUnknownMemberType]


def test_quiesce_all_agents_returns_false_on_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    """A quiesce that times out (stragglers still live) returns False so the
    orchestration force-reaps them on every host instead of proceeding blind."""
    monkeypatch.setattr(_up, "_quiesce_pass", lambda _signalled: False)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_up, "time", _FakeTime())

    assert _up._quiesce_all_agents(timeout_s=0.05) is False


class _FakeTime:
    """time.monotonic()/sleep() pair whose sleep jumps the clock a full second —
    so a poll loop with a ~0s deadline exits after two rounds."""

    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now

    def sleep(self, _s: float) -> None:
        self.now += 1.0


def test_force_mode_orchestration_force_reaps_every_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`--mode force` (or any quiesce timeout) makes the rollout force-reap
    stragglers: the gateway's local leg reaps this host's live agents and Phase B
    carries force_reap to every agent-runner's updater."""
    seen: dict[str, object] = {}
    monkeypatch.setattr(_cli, "_changed_paths_vs_origin", lambda: ["gateway/app.py"])
    monkeypatch.setattr(_up, "_vet_rollout_target", lambda _sha: None)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_cli, "_list_agent_runners", lambda: [("a", None)])
    monkeypatch.setattr(
        _cli,
        "_quiesce_all_agents",
        lambda **_: (
            seen.update({"quiesced": False}) or False
        ),  # stragglers stay live  # pyright: ignore[reportUnknownArgumentType]
    )
    monkeypatch.setattr(
        _cli,
        "_run_gateway_local_update",
        lambda _repo, **_kw: seen.update({"local_force_reap": _kw.get("force_reap_agents")}) or 0,  # pyright: ignore[reportUnknownArgumentType, reportUnknownMemberType]
    )

    def _phase_b(_hosts, **kw):  # type: ignore[no-untyped-def]
        seen.update({"phaseb_force_reap": kw.get("force_reap")})  # pyright: ignore[reportUnknownArgumentType, reportUnknownMemberType]
        return 0, _up.RolloutOutcome.CLEAN, []

    monkeypatch.setattr(_up, "_phase_b_outcome", _phase_b)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_cli, "_fan_out", lambda *_a, **_k: [("a", "ok", "")])  # pyright: ignore[reportUnknownArgumentType]

    rc = _cli._run_gateway_orchestration(Path("/unused"), origin="test-origin", mode="force")
    assert rc == 0
    assert seen["quiesced"] is False
    assert seen["local_force_reap"] is True
    assert seen["phaseb_force_reap"] is True


def test_smooth_mode_does_not_force_reap_when_all_drained(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Smooth mode that fully drains (no stragglers) does NOT force-reap — the
    agents exited cleanly at their turn boundaries, nothing to kill."""
    seen: dict[str, object] = {}
    monkeypatch.setattr(_cli, "_changed_paths_vs_origin", lambda: ["gateway/app.py"])
    monkeypatch.setattr(_up, "_vet_rollout_target", lambda _sha: None)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_cli, "_list_agent_runners", list)
    monkeypatch.setattr(_cli, "_quiesce_all_agents", lambda **_: True)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(
        _cli,
        "_run_gateway_local_update",
        lambda _repo, **_kw: seen.update({"local_force_reap": _kw.get("force_reap_agents")}) or 0,  # pyright: ignore[reportUnknownArgumentType, reportUnknownMemberType]
    )

    rc = _cli._run_gateway_orchestration(Path("/unused"), origin="test-origin")
    assert rc == 0
    assert seen["local_force_reap"] is False


def test_quiesce_local_agents_signals_only_this_machine(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The per-host quiesce (agent-runner self-update / `ava restart --quiesce`)
    signals this machine's live agents with source='system:update' and drains
    them; mode='none' (rollout Phase B) is a no-op."""
    import shared.db as db_mod
    from shared import machine as machine_mod

    monkeypatch.setattr(machine_mod, "machine_name", lambda: "test-machine")
    signalled: list[tuple[str, object]] = []

    def _signal(source: str, *, exclude_agent_ids=(), machine=None):  # type: ignore[no-untyped-def]
        signalled.append((source, machine))  # pyright: ignore[reportUnknownArgumentType]
        return [1]

    monkeypatch.setattr(db_mod, "signal_live_agents_restart", _signal)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(db_mod, "list_live_agent_ids", lambda **_kwargs: [])  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_up, "time", _FakeTime())
    monkeypatch.setattr(_up, "_quiesce_timeout_s", lambda _mode: 0.05)  # pyright: ignore[reportUnknownArgumentType]

    # mode none: no signal, no wait
    assert _up._quiesce_local_agents("none") is True
    assert signalled == []

    # mode smooth: signals this machine and drains
    assert _up._quiesce_local_agents("smooth") is True
    assert signalled == [("system:update", "test-machine")]

    # mode force: signals, waits the short window, force-reaps the straggler
    monkeypatch.setattr(db_mod, "list_live_agent_ids", lambda **_kwargs: [1])  # pyright: ignore[reportUnknownArgumentType]
    reaped: list[str] = []
    monkeypatch.setattr(_up, "_force_reap_local_agents", lambda: reaped.append("reaped") or [])
    assert _up._quiesce_local_agents("force") is False
    assert reaped == ["reaped"]


def test_force_reap_local_agents_marks_and_kills(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Force-reap CAS-marks this host's live agents 'restarting' (so the
    restarter respawns them on new code) and kills their processes."""
    import shared.db as db_mod
    from shared import machine as machine_mod

    monkeypatch.setattr(machine_mod, "machine_name", lambda: "test-machine")
    monkeypatch.setattr(db_mod, "list_live_agent_ids", lambda **_kwargs: [7, 8])  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(db_mod, "mark_agents_restarting", lambda ids: ids)  # pyright: ignore[reportUnknownArgumentType]
    reaped: list[dict] = []
    monkeypatch.setattr(_cli, "_reap_agent_sessions", lambda **_kw: reaped.append(_kw) or [])  # pyright: ignore[reportUnknownArgumentType, reportUnknownMemberType]

    assert _up._force_reap_local_agents() == [7, 8]
    # kill_shells=False: the force-reap kills agent PROCESSES only — the
    # persistent shell / watcher sessions survive the update (#1055, user
    # ruling 2026-08-08: rollout must not reap background sessions).
    assert reaped == [{"kill_shells": False}]


def test_force_reap_local_agents_noop_when_none_live(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import shared.db as db_mod
    from shared import machine as machine_mod

    monkeypatch.setattr(machine_mod, "machine_name", lambda: "test-machine")
    monkeypatch.setattr(db_mod, "list_live_agent_ids", lambda **_kwargs: [])  # pyright: ignore[reportUnknownArgumentType]
    reaped: list[str] = []
    monkeypatch.setattr(_cli, "_reap_agent_sessions", lambda **_kw: reaped.append("reap") or [])  # pyright: ignore[reportUnknownArgumentType]

    assert _up._force_reap_local_agents() == []
    assert reaped == []
