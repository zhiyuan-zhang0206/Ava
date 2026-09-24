"""The hierarchy worker's trigger + guardrails (task #4674).

The event trigger is the worker's primary path: each compact boundary
enqueues its own build job (`shared/agents/history/checkpoint_cleanup.py`),
and `runner.run_tick` consumes — claim, drain, done. These pin the consuming
side: the tick's drain/scan cadence (the moved `run_tick` tests), the
claim-time semantics (silent baseline for a never-tracked agent, the
first-build backfill), the master switch, and the §4 guardrails (the 24h
budget breaker, the halt marker, the done-time signals on the child's side).
The scan's enqueue decisions and the child-side outcome live in
`tests/services/test_hierarchy_worker.py`; the enqueue SQL itself in
`tests/test_checkpoint_cleanup.py`.
"""

from __future__ import annotations

from collections.abc import Callable

import psycopg
import pytest

from services.hierarchy_worker import execute as execute_module
from services.hierarchy_worker import runner
from services.hierarchy_worker.scan import ScanOutcome, scan
from shared.agents.history.hierarchy.pipeline import MaterializedTree
from shared.config import settings
from shared.events.contract import telemetry_events


def cid(nth: int) -> str:
    """Lexicographically ordered UUIDv6-shaped checkpoint ids, one per `nth`."""
    return f"1f1b2202-0000-6000-a3e4-{nth:012x}"


def _boundary(conn: psycopg.Connection, agent_id: int, nth: int) -> str:
    """Insert one compact-boundary checkpoint row for `agent_id`."""
    checkpoint_id = cid(nth)
    conn.execute(
        "INSERT INTO checkpoints (thread_id, checkpoint_ns, checkpoint_id, checkpoint, metadata)"
        " VALUES (%s, '', %s, '{}'::jsonb, '{\"compact_boundary\": true}'::jsonb)",
        (str(agent_id), checkpoint_id),
    )
    conn.commit()
    return checkpoint_id


def _state(conn: psycopg.Connection, agent_id: int, boundary: str) -> None:
    conn.execute(
        "INSERT INTO hierarchy_worker_state (agent_id, last_processed_boundary) VALUES (%s, %s)",
        (agent_id, boundary),
    )
    conn.commit()


def _pending_job(conn: psycopg.Connection, agent_id: int, boundary: str) -> int:
    """Insert one pending compact job exactly as the event trigger writes it."""
    row = conn.execute(
        "INSERT INTO hierarchy_jobs (agent_id, kind, trigger_boundary, status, include_tail)"
        " VALUES (%s, 'compact', %s, 'pending', false) RETURNING id",
        (agent_id, boundary),
    ).fetchone()
    assert row is not None
    conn.commit()
    return int(row[0])


def _hot_row(conn: psycopg.Connection, agent_id: int, generated: int) -> None:
    """One finished job row contributing `generated` nodes to the 24h window."""
    conn.execute(
        "INSERT INTO hierarchy_jobs (agent_id, kind, trigger_boundary, status, include_tail,"
        " generated, finished_at) VALUES (%s, 'compact', %s, 'done', false, %s, now())",
        (agent_id, cid(1), generated),
    )
    conn.commit()


def _record_emit(emitted: list[tuple[str, dict[str, object]]]) -> Callable[..., None]:
    """A `shared.telemetry.emit` stand-in that mirrors the fail-fast contract —
    an unregistered kind raises in production, so it must raise here too, or a
    stray name would pass silently in tests (the orphaned-kind gate's blind
    spot)."""

    def emit(_category: str, event_name: str, **kwargs: object) -> None:
        assert event_name in telemetry_events(), f"unregistered event kind: {event_name}"
        attributes = kwargs.get("attributes")
        emitted.append((event_name, dict(attributes) if isinstance(attributes, dict) else {}))

    return emit


class _FakeConnection:
    """Just enough of `connect(autocommit=True)`'s context manager for ticks."""

    def __enter__(self) -> _FakeConnection:
        return self

    def __exit__(self, *exc: object) -> bool:
        return False


def _fake_connect(**_kw: object) -> _FakeConnection:
    return _FakeConnection()


def _armed_tick(monkeypatch: pytest.MonkeyPatch) -> None:
    """Arm a fake-connection tick test: switch on, first scan due, breaker quiet."""
    monkeypatch.setattr(settings.daemon, "hierarchy_worker_enabled", True)
    monkeypatch.setattr(runner, "_fallback_scanned_at", None)
    monkeypatch.setattr(runner, "_regen_budget_check", lambda _conn: False)


# ---- the tick: drain semantics, scan cadence, switch ----


def test_run_tick_drains_back_to_back_and_scans_once_per_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A tick drains every due job before returning; the reconcile scan runs
    once when due (the first tick after boot), not once per claim — the event
    trigger, not the scan, is what keeps the queue fed (task #4674 B2/B3)."""
    scanned: list[int] = []
    ran: list[int] = []
    jobs = [
        runner.ClaimedJob(id=1, agent_id=11, include_tail=True),
        runner.ClaimedJob(id=2, agent_id=12, include_tail=False),
    ]

    def fake_scan(conn: object) -> ScanOutcome:
        scanned.append(len(scanned))
        return ScanOutcome()

    def fake_claim(conn: object) -> runner.ClaimedJob | None:
        return jobs.pop(0) if jobs else None

    _armed_tick(monkeypatch)
    monkeypatch.setattr(runner, "connect", _fake_connect)
    monkeypatch.setattr(runner, "scan", fake_scan)
    monkeypatch.setattr(runner, "claim_next", fake_claim)

    def fake_child(job: runner.ClaimedJob) -> None:
        ran.append(job.id)

    monkeypatch.setattr(runner, "run_child", fake_child)

    runner.run_tick()
    assert ran == [1, 2]
    assert len(scanned) == 1  # one due scan, then the claims drain

    runner.run_tick()  # inside the fallback window: no re-scan, nothing due
    assert len(scanned) == 1


def test_run_tick_returns_on_a_transient_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """A transient DB failure ends the tick without raising — the next slot
    retries, so the manager never sees a crash for a blip."""

    def failing_scan(conn: object) -> ScanOutcome:
        raise RuntimeError("db unavailable")

    _armed_tick(monkeypatch)
    monkeypatch.setattr(runner, "connect", _fake_connect)
    monkeypatch.setattr(runner, "scan", failing_scan)

    runner.run_tick()  # returns — no exception escapes the tick


def test_run_tick_raises_on_schema_drift(monkeypatch: pytest.MonkeyPatch) -> None:
    """Code<->DB drift escapes the tick so the manager's crash path records it."""

    def drifted_scan(conn: object) -> ScanOutcome:
        raise psycopg.ProgrammingError('relation "hierarchy_jobs" does not exist')

    _armed_tick(monkeypatch)
    monkeypatch.setattr(runner, "connect", _fake_connect)
    monkeypatch.setattr(runner, "scan", drifted_scan)

    with pytest.raises(psycopg.ProgrammingError):
        runner.run_tick()


def test_run_tick_is_silent_while_the_switch_is_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """The master switch (task #4674 B5) gates the worker side too: off means
    the tick touches nothing — not the breaker, not the scan, not the claim."""
    touched: list[str] = []

    def exploding_connect(**_kw: object) -> object:
        touched.append("connect")
        raise AssertionError("switch off must not open a connection")

    monkeypatch.setattr(runner, "connect", exploding_connect)
    assert settings.daemon.hierarchy_worker_enabled is False  # the shipped default

    runner.run_tick()
    assert touched == []


# ---- claim-time semantics (B4): silent baseline + first-build backfill ----


def test_claim_silent_baselines_a_never_tracked_agent(db_conn: psycopg.Connection) -> None:
    """An event job for an agent the scan has not seen yet baselines silently:
    the boundary is recorded, the job is retired with the marker, and the
    drain moves on — nothing builds for pre-existing history (task #4674 B4)."""
    agent_id = 880_100
    job_id = _pending_job(db_conn, agent_id, cid(1))

    assert runner.claim_next(db_conn) is None  # baselined in passing, not claimed

    state = db_conn.execute(
        "SELECT last_processed_boundary FROM hierarchy_worker_state WHERE agent_id = %s",
        (agent_id,),
    ).fetchone()
    assert state == (cid(1),)
    row = db_conn.execute(
        "SELECT status, error FROM hierarchy_jobs WHERE id = %s", (job_id,)
    ).fetchone()
    assert row is not None and row[0] == "done" and "silent baseline" in str(row[1])


def test_claim_backfills_include_tail_for_the_first_build(db_conn: psycopg.Connection) -> None:
    """An event-enqueued job always lands with include_tail=false — only the
    claim can read `scan.first_build` — so the claim backfills it: the first
    build of a tracked-but-never-built agent seals its tail (task #4674)."""
    agent_id = 880_101
    _state(db_conn, agent_id, cid(1))
    job_id = _pending_job(db_conn, agent_id, cid(1))

    claimed = runner.claim_next(db_conn)
    assert claimed is not None and claimed.id == job_id and claimed.include_tail is True
    row = db_conn.execute(
        "SELECT include_tail FROM hierarchy_jobs WHERE id = %s", (job_id,)
    ).fetchone()
    assert row == (True,)


def test_claim_leaves_include_tail_false_after_a_build(db_conn: psycopg.Connection) -> None:
    """After a clean build exists the first-build mode is over: the claim
    leaves include_tail=false and the run keeps the tail pending (task #4674)."""
    agent_id = 880_102
    _state(db_conn, agent_id, cid(1))
    db_conn.execute(
        "INSERT INTO hierarchy_jobs (agent_id, kind, trigger_boundary, status, include_tail,"
        " failed, skipped) VALUES (%s, 'compact', %s, 'done', false, 0, 0)",
        (agent_id, cid(1)),
    )
    job_id = _pending_job(db_conn, agent_id, cid(2))

    claimed = runner.claim_next(db_conn)
    assert claimed is not None and claimed.id == job_id and claimed.include_tail is False


# ---- §4: the 24h budget breaker ----


def test_regen_budget_trips_on_the_edge_and_stops_claiming(
    db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The 24h window crossing the budget trips the persistent breaker once
    (the edge — later readings while the trip stands rewrite nothing), and the
    tick stops claiming until an operator resets it (task #4674 §4)."""
    monkeypatch.setattr(settings.daemon, "hierarchy_regen_daily_budget_nodes", 3)
    emitted: list[tuple[str, dict[str, object]]] = []
    monkeypatch.setattr("shared.telemetry.emit", _record_emit(emitted))
    _hot_row(db_conn, 880_103, 4)
    # A row finished outside the 24h window does not count toward it.
    db_conn.execute(
        "INSERT INTO hierarchy_jobs (agent_id, kind, trigger_boundary, status, include_tail,"
        " generated, finished_at)"
        " VALUES (880_103, 'compact', %s, 'done', false, 100, now() - interval '25 hours')",
        (cid(1),),
    )
    db_conn.commit()

    assert runner._regen_budget_check(db_conn) is True
    trips = [e for e in emitted if e[0] == "hierarchy_regen_budget_tripped"]
    assert trips == [("hierarchy_regen_budget_tripped", {"window_nodes": 4, "budget_nodes": 3})]

    # The edge: a second reading under the unreset trip emits nothing new.
    assert runner._regen_budget_check(db_conn) is True
    assert len([e for e in emitted if e[0] == "hierarchy_regen_budget_tripped"]) == 1


def test_run_tick_stops_while_the_breaker_is_tripped(monkeypatch: pytest.MonkeyPatch) -> None:
    """An active trip stops the tick before any claim (task #4674 §4): the
    worker stays down until the operator resets the breaker."""
    claims: list[str] = []

    _armed_tick(monkeypatch)
    monkeypatch.setattr(runner, "connect", _fake_connect)
    monkeypatch.setattr(runner, "_regen_budget_check", lambda _conn: True)
    monkeypatch.setattr(runner, "claim_next", lambda _conn: claims.append("claim"))

    runner.run_tick()
    assert claims == []


def test_budget_reset_lifts_the_stop_and_a_cooled_window_rearms(
    db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Post-trip life cycle (task #4674 §4): an operator reset lifts the stop
    immediately (the hot window may continue — the operator owns that call),
    the first reading at or below budget re-arms the breaker, and only then
    can a new excursion trip it again."""
    monkeypatch.setattr(settings.daemon, "hierarchy_regen_daily_budget_nodes", 3)
    emitted: list[tuple[str, dict[str, object]]] = []
    monkeypatch.setattr("shared.telemetry.emit", _record_emit(emitted))
    _hot_row(db_conn, 880_103, 4)

    assert runner._regen_budget_check(db_conn) is True  # trip

    db_conn.execute(
        "UPDATE hierarchy_worker_breaker SET reset_at = now(), reset_note = 'test' WHERE id = 1"
    )
    db_conn.commit()
    assert runner._regen_budget_check(db_conn) is False  # the reset lifts the stop...
    assert len([e for e in emitted if e[0] == "hierarchy_regen_budget_tripped"]) == 1
    row = db_conn.execute(
        "SELECT reset_at IS NULL, rearmed_at IS NOT NULL FROM hierarchy_worker_breaker WHERE id = 1"
    ).fetchone()
    assert row == (False, False)  # ...but a hot window cannot re-trip before re-arming

    db_conn.execute("DELETE FROM hierarchy_jobs")  # the window cools
    db_conn.commit()
    assert runner._regen_budget_check(db_conn) is False
    row = db_conn.execute(
        "SELECT rearmed_at IS NOT NULL FROM hierarchy_worker_breaker WHERE id = 1"
    ).fetchone()
    assert row == (True,)  # the cooled reading re-armed it

    _hot_row(db_conn, 880_104, 4)  # a fresh excursion...
    assert runner._regen_budget_check(db_conn) is True  # ...trips again
    assert len([e for e in emitted if e[0] == "hierarchy_regen_budget_tripped"]) == 2


def test_halt_marker_routes_the_continuation_to_backoff(db_conn: psycopg.Connection) -> None:
    """A halt-marked done row is not a pure continuation (task #4674): its
    error text routes the retry through the exponential backoff instead of the
    immediate drain — a runaway wave must not hot-loop (the error-less
    continuation fast path is pinned in the worker tests)."""
    agent_id = 880_105
    _boundary(db_conn, agent_id, 1)
    scan(db_conn)
    _boundary(db_conn, agent_id, 2)
    db_conn.execute(
        "UPDATE hierarchy_worker_state SET last_processed_boundary = %s WHERE agent_id = %s",
        (cid(1), agent_id),
    )
    db_conn.execute(
        "INSERT INTO hierarchy_jobs (agent_id, kind, trigger_boundary, status, include_tail,"
        " failed, skipped, error, finished_at)"
        " VALUES (%s, 'compact', %s, 'done', false, 0, 5, 'regen halt: generated 400 reached"
        " hierarchy_regen_halt_nodes_per_job=400; remainder skipped', now())",
        (agent_id, cid(2)),
    )
    db_conn.commit()
    _boundary(db_conn, agent_id, 3)

    assert scan(db_conn).enqueued == 0  # inside the backoff window

    db_conn.execute(
        "UPDATE hierarchy_jobs SET finished_at = now() - interval '2 hours' WHERE agent_id = %s",
        (agent_id,),
    )
    db_conn.commit()
    assert scan(db_conn).enqueued == 1  # past the base backoff, the retry lands


# ---- the child side: the halt marker + the done-time signals ----


def test_halted_build_records_the_marker_and_emits_the_signals(
    db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A halted run lands `done` with the halt text as its error — the marker
    scan reads to back the continuation off — emits the halt event, and the
    done-time signals ride the same completion (task #4674)."""
    agent_id = 880_106
    _state(db_conn, agent_id, cid(1))
    db_conn.execute(
        "INSERT INTO hierarchy_jobs (agent_id, kind, trigger_boundary, status, include_tail,"
        " failed, skipped) VALUES (%s, 'compact', %s, 'done', false, 0, 0)",
        (agent_id, cid(1)),
    )
    job_id = _pending_job(db_conn, agent_id, cid(1))
    assert runner.claim_next(db_conn) is not None
    db_conn.commit()

    emitted: list[tuple[str, dict[str, object]]] = []
    monkeypatch.setattr("shared.telemetry.emit", _record_emit(emitted))
    seen_caps: list[object] = []
    halted = MaterializedTree(
        nodes=(), errors=(), pending={}, max_level=1, generated=400, skipped=2, halted=True
    )

    def fake_tree(*args: object, **kwargs: object) -> MaterializedTree:
        seen_caps.append(kwargs.get("max_generated"))
        return halted

    monkeypatch.setattr(execute_module, "load_known_texts", lambda _aid: {})
    monkeypatch.setattr(execute_module, "build_generation_llm", lambda _model: object())
    monkeypatch.setattr(execute_module, "close_chat_model", lambda _llm: None)
    monkeypatch.setattr(execute_module, "build_agent_tree", fake_tree)
    monkeypatch.setattr(execute_module, "write_tree", lambda *_a, **_k: 0)

    assert execute_module.execute_job(job_id) == 0
    assert seen_caps == [settings.daemon.hierarchy_regen_halt_nodes_per_job]
    row = db_conn.execute(
        "SELECT status, error FROM hierarchy_jobs WHERE id = %s", (job_id,)
    ).fetchone()
    assert row is not None and row[0] == "done" and "regen halt" in str(row[1])
    assert [e[0] for e in emitted] == [
        "hierarchy_regen_halt",
        "hierarchy_regen_alert",
        "hierarchy_regen_low_reuse",
    ]


def test_regen_signals_fire_only_past_their_thresholds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The done-time signals (task #4674 §4): the size alert fires past the
    alert threshold, the low-reuse alert when an established tree is cut
    without reusing it; an ordinary small high-reuse run emits nothing."""
    monkeypatch.setattr(settings.daemon, "hierarchy_regen_alert_nodes_per_job", 3)
    monkeypatch.setattr(settings.daemon, "hierarchy_regen_min_reuse_ratio", 0.5)
    emitted: list[tuple[str, dict[str, object]]] = []
    monkeypatch.setattr("shared.telemetry.emit", _record_emit(emitted))

    cut = MaterializedTree(nodes=(), errors=(), pending={}, max_level=1, generated=4, reused=0)
    execute_module._regen_signals(7, 9, cut)
    assert [e[0] for e in emitted] == ["hierarchy_regen_alert", "hierarchy_regen_low_reuse"]
    assert emitted[0][1] == {"agent_id": 7, "job_id": 9, "generated": 4, "threshold": 3}

    emitted.clear()
    ordinary = MaterializedTree(
        nodes=(), errors=(), pending={}, max_level=1, generated=2, reused=50
    )
    execute_module._regen_signals(7, 10, ordinary)
    assert emitted == []


def test_guardrail_emit_failure_never_fails_the_build(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The guardrail events observe a completed build, so an emission failure
    is swallowed (a warning), never a raise — it must not corrupt the job's
    own record (task #4674)."""

    def broken(_category: str, _event_name: str, **_kwargs: object) -> None:
        raise RuntimeError("sink down")

    monkeypatch.setattr("shared.telemetry.emit", broken)

    execute_module._try_emit("hierarchy_regen_halt", {"agent_id": 1})  # no raise
