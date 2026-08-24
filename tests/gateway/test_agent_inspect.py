"""Tests for GET /api/agents/{id}/inspect HTTP (task #1197: Loki read side).

Locks the per-agent inspector panel's query contract — config overlay
passthrough, cumulative LLM cost (no time window, accumulated since spawn),
turn/exec stat field names + exec ok/fail split. The event-history aggregates
now read Loki via `gateway/loki_events.py`; these tests install an in-memory
`_FakeLoki` (autouse) that serves canned event rows through the real route
plumbing — cost pricing, alive-time replay, heartbeat projection and window
handling stay real. The LogQL building / aggregation math is covered by
`tests/gateway/test_loki_events.py`; this file locks the route semantics.

`shells` is probed via the `shell_probe` cluster op dispatched to the agent's
machine (uniform path — the gateway never runs sessions itself). The test
environment has no registered machines, so an agent with machine='unknown'
degrades to an empty list; the dispatch contract itself (remote shells
surfaced, unreachable machine degraded) is covered below.
"""

from __future__ import annotations

import asyncio
import json
import re
import threading
import time
from collections import Counter
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

import httpx
import psycopg
import pytest
from fastapi.testclient import TestClient

from gateway import loki_events, loki_query_budget
from gateway.app import app
from gateway.loki_events import _weighted_quantile
from gateway.routers import _agent_cost, _inspect_stats, agent_inspect
from gateway.routers._inspect_cache import InspectCacheFullError, InspectQueryCache
from shared.config import settings
from shared.loki_index_labels import INDEX_LABEL_CUTOVER_AT


def _insert_agent_row(db: psycopg.Connection, label: str = "t") -> int:
    with db.cursor() as cur:
        cur.execute("INSERT INTO agents (label) VALUES (%s) RETURNING id", (label,))
        row = cur.fetchone()
    assert row is not None, "INSERT ... RETURNING must return one row"
    return row[0]


def _insert_agent(
    db: psycopg.Connection,
    *,
    status: str = "running",
    config_overlay: dict | None = None,  # pyright: ignore[reportMissingTypeArgument, reportUnknownParameterType]
    status_changed_s_ago: float | None = None,
    paused_until_s_ahead: float | None = None,
) -> int:
    """INSERT an agents_meta row. `status_changed_s_ago` backdates BOTH
    status_changed_at and last_active_at (the BEFORE-UPDATE-OF-status trigger does
    not fire on a timestamp-only update) — it models "the agent last did anything
    N seconds ago", and the heartbeat projection reads the real-activity clock
    (last_active_at). `paused_until_s_ahead` sets heartbeat_paused_until relative
    to now() — negative = an already-expired pause."""
    tid = _insert_agent_row(db)
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO agents_meta (id, spawner, status, config_overlay) "
            "VALUES (%s, 'user', %s, %s::jsonb)",
            (tid, status, json.dumps(config_overlay) if config_overlay is not None else None),
        )
        if status_changed_s_ago is not None:
            cur.execute(
                "UPDATE agents_meta SET status_changed_at = now() - make_interval(secs => %s), "
                "       last_active_at = now() - make_interval(secs => %s) "
                "WHERE id = %s",
                (status_changed_s_ago, status_changed_s_ago, tid),
            )
        if paused_until_s_ahead is not None:
            cur.execute(
                "UPDATE agents_meta SET heartbeat_paused_until = now() + make_interval(secs => %s) "
                "WHERE id = %s",
                (paused_until_s_ahead, tid),
            )
    return tid


def _seconds_from_now(iso: str) -> float:
    """Signed seconds between an ISO-8601 instant and now (future = positive)."""
    return (datetime.fromisoformat(iso) - datetime.now(UTC)).total_seconds()


class _FakeLoki:
    """In-memory stand-in for `gateway.loki_events` — the inspector route's
    event-history queries run against this instead of a real Loki. Tests add
    rows with `add(...)` (the same shape the old `events` INSERTs had) and the
    fake honors the same filter/window/paging semantics the route relies on
    (categories, event_name regex, attribute filters, from_/to, newest-first,
    +1 lookahead has_more), plus Loki's real `max_query_length` guardrail
    (30d1h): a window starting earlier raises, like the real 400 the gateway
    surfaces as a 500 — the CI gap that let the whole-life inspector 500 ship."""

    # Loki's real max_query_length default (30d1h): a query whose window
    # starts earlier gets a 400, and the gateway surfaces it as a 500. The
    # fake models it so whole-life windows stay within `_whole_life_start()`.
    _LOKI_MAX_QUERY_AGE = timedelta(days=30, hours=1)

    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []
        self.wire_calls: Counter[str] = Counter()
        self.projected_calls: list[dict[str, Any]] = []

    def _record_wire_call(self, method: str) -> None:
        self.wire_calls[method] += 1

    def add(
        self,
        *,
        event: str,
        agent_id: int,
        payload: dict[str, Any] | None = None,
        ts_offset_hours: float = 0,
        ts: datetime | None = None,
        category: str = "telemetry",
    ) -> None:
        self.rows.append(
            {
                "id": len(self.rows) + 1,
                "ts": ts
                if ts is not None
                else datetime.now(UTC) - timedelta(hours=ts_offset_hours),
                "agent_id": agent_id,
                "machine": "test",
                "process": "test",
                "category": category,
                "event_name": event,
                "level": "info",
                "source": "test",
                "target_agent_id": None,
                "attributes": payload or {},
            }
        )

    def _match(self, **kwargs: Any) -> list[dict[str, Any]]:
        from_ = kwargs.get("from_")
        if from_ is not None and datetime.now(UTC) - from_ > _FakeLoki._LOKI_MAX_QUERY_AGE:
            raise AssertionError(
                f"Loki rejects this window (max_query_length=30d1h): from_={from_.isoformat()}"
            )
        out: list[dict[str, Any]] = []
        for r in self.rows:
            if kwargs.get("agent_id") is not None and r["agent_id"] != kwargs["agent_id"]:
                continue
            categories = kwargs.get("categories")
            if categories and r["category"] not in categories:
                continue
            event_names = kwargs.get("event_names")
            if event_names and not any(
                re.search(e, r["event_name"])
                for e in event_names  # type: ignore[arg-type]
            ):
                continue
            matched = True
            for key, want in (kwargs.get("attribute_filters") or {}).items():  # type: ignore[union-attr]
                # normalize like jsonb `->>'key'` does: True -> "true", "true" -> "true"
                raw = r["attributes"].get(key, "")
                got = json.dumps(raw).strip('"') if raw != "" else ""
                want_s = str(want)  # type: ignore[arg-type]
                if want_s.startswith("!="):
                    if got == want_s[2:]:
                        matched = False
                        break
                elif got != want_s:
                    matched = False
                    break
            if not matched:
                continue
            if kwargs.get("from_") is not None and r["ts"] < kwargs["from_"]:
                continue
            if kwargs.get("to") is not None and r["ts"] > kwargs["to"]:
                continue
            if kwargs.get("grep") and kwargs["grep"] not in json.dumps(r, default=str):
                continue
            out.append(r)
        return out

    def query_events(self, **kwargs: Any) -> tuple[list[dict[str, Any]], bool]:
        self._record_wire_call("query_events")
        rows = sorted(self._match(**kwargs), key=lambda r: r["ts"], reverse=True)
        limit = kwargs.get("limit", 100)
        offset = kwargs.get("offset", 0)
        page = rows[offset : offset + limit]
        return page, len(rows) > offset + limit

    def count_events(self, **kwargs: Any) -> int:
        self._record_wire_call("count_events")
        return len(self._match(**kwargs))

    def count_by_event_name(self, **kwargs: Any) -> dict[str, int]:
        self._record_wire_call("count_by_event_name")
        counts: dict[str, int] = {}
        for row in self._match(**kwargs):
            event_name = str(row["event_name"])
            counts[event_name] = counts.get(event_name, 0) + 1
        return counts

    def attribute_distribution(self, **kwargs: Any) -> list[tuple[float, int]]:
        self._record_wire_call("attribute_distribution")
        field = kwargs["field"]
        counts: dict[float, int] = {}
        for row in self._match(**kwargs):
            raw = row["attributes"].get(field)
            if raw is None:
                continue
            value = float(raw)
            counts[value] = counts.get(value, 0) + 1
        return sorted(counts.items())

    def attribute_aggregate(self, **kwargs: Any) -> Any:
        self._record_wire_call("attribute_aggregate")
        rows = self._match(**kwargs)
        field = kwargs["field"]
        agg = kwargs["agg"]
        group_by = kwargs.get("group_by")

        def _value(r: dict[str, Any]) -> float | None:
            v = r["attributes"].get(field)
            return float(v) if v is not None else None

        def _agg(vs: list[float]) -> float:
            if not vs:
                return 0.0
            if agg == "sum":
                return sum(vs)
            if agg == "min":
                return min(vs)
            if agg == "max":
                return max(vs)
            if agg == "count":
                return float(len(vs))
            if agg == "quantile":
                dist = sorted((v, vs.count(v)) for v in set(vs))
                return _weighted_quantile(kwargs["quantile"], dist)
            raise AssertionError(f"fake agg {agg}")

        if group_by:
            groups: dict[str, list[float]] = {}
            for r in rows:
                g = str(r["attributes"].get(group_by, ""))
                v = _value(r)
                if v is not None:
                    groups.setdefault(g, []).append(v)
            return [(g, _agg(vs)) for g, vs in groups.items()]
        return _agg([v for v in (_value(r) for r in rows) if v is not None])

    def query_projected_lines(self, **kwargs: Any) -> list[tuple[int, int | None, str]]:
        """Return the raw event body format the inspector reduces client-side."""
        self._record_wire_call("query_projected_lines")
        self.projected_calls.append(kwargs)
        assert kwargs["fields"] == []
        assert kwargs["template"] == "{{ __line__ }}"
        rows: list[tuple[int, int | None, str]] = []
        for row in self._match(**kwargs):
            body = {
                "ts": row["ts"].isoformat(),
                "agent_id": row["agent_id"],
                "machine": row["machine"],
                "process": row["process"],
                "category": row["category"],
                "event_name": row["event_name"],
                "level": row["level"],
                "source": row["source"],
                "target_agent_id": row["target_agent_id"],
                "attributes": row["attributes"],
            }
            rows.append(
                (
                    int(row["ts"].timestamp() * 1e9),
                    row["agent_id"],
                    json.dumps(body, separators=(",", ":")),
                )
            )
        return sorted(rows)


@pytest.fixture(autouse=True)
def fake_loki(monkeypatch: pytest.MonkeyPatch) -> _FakeLoki:
    """Route all loki_events calls through an in-memory fake; each test gets
    an empty store and adds its own rows. The cost + inspect TTL caches are
    cleared so no test serves another test's cached aggregates."""
    fake = _FakeLoki()
    monkeypatch.setattr(loki_events, "query_events", fake.query_events)
    monkeypatch.setattr(loki_events, "count_events", fake.count_events)
    monkeypatch.setattr(loki_events, "count_by_event_name", fake.count_by_event_name)
    monkeypatch.setattr(loki_events, "attribute_distribution", fake.attribute_distribution)
    monkeypatch.setattr(loki_events, "attribute_aggregate", fake.attribute_aggregate)
    monkeypatch.setattr(loki_events, "query_projected_lines", fake.query_projected_lines)
    _agent_cost.cache_clear()
    agent_inspect.cache_clear()
    return fake


def _lifecycle_line(ts: datetime, event_name: str = "agent_spawned") -> tuple[int, int, str]:
    body = {
        "ts": ts.isoformat(),
        "agent_id": 7,
        "machine": "test",
        "process": "test",
        "category": "audit",
        "event_name": event_name,
        "level": "info",
        "source": "test",
        "target_agent_id": None,
        "attributes": {},
    }
    return (int(ts.timestamp() * 1e9), 7, json.dumps(body, separators=(",", ":")))


def _lifecycle_projected_calls(fake_loki: _FakeLoki) -> list[dict[str, Any]]:
    return [
        call
        for call in fake_loki.projected_calls
        if call["event_names"] == ["^agent_spawned$", "^agent_resurrected$", "^agent_terminated$"]
    ]


def test_lifecycle_cache_is_per_agent_not_inspect_window_and_expires(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The retained lifecycle load is shared by h=1/h=24 response caches."""
    now = datetime(2026, 8, 23, tzinfo=UTC)
    window = (now - timedelta(days=7), now)
    calls: list[dict[str, Any]] = []
    clock = [0.0]

    def projected(**kwargs: Any) -> list[tuple[int, int, str]]:
        calls.append(kwargs)
        return [_lifecycle_line(now)]

    monkeypatch.setattr(loki_events, "query_projected_lines", projected)
    monkeypatch.setattr(_inspect_stats.time_mod, "monotonic", lambda: clock[0])

    first = _inspect_stats.cached_live_lifecycle(7, window=window, freeze=now)
    # h=1 and h=24 calculate this same retained lifecycle window; the cache
    # key deliberately remains only the agent id.
    clock[0] = 301.0
    second = _inspect_stats.cached_live_lifecycle(7, window=window, freeze=now)
    clock[0] = 1801.0
    third = _inspect_stats.cached_live_lifecycle(7, window=window, freeze=now)

    assert first == second == third == [(now, "agent_spawned")]
    assert len(calls) == 2
    assert calls[0]["timeout_s"] == 8.0


def test_lifecycle_cache_cold_miss_is_single_flight(monkeypatch: pytest.MonkeyPatch) -> None:
    """Concurrent views of one agent wait for the same lifecycle scan."""
    now = datetime(2026, 8, 23, tzinfo=UTC)
    window = (now - timedelta(days=7), now)
    entered = threading.Event()
    release = threading.Event()
    calls = 0

    def projected(**_kwargs: Any) -> list[tuple[int, int, str]]:
        nonlocal calls
        calls += 1
        entered.set()
        assert release.wait(timeout=2)
        return [_lifecycle_line(now)]

    monkeypatch.setattr(loki_events, "query_projected_lines", projected)
    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(
            _inspect_stats.cached_live_lifecycle,
            7,
            window=window,
            freeze=now,
        )
        assert entered.wait(timeout=2)
        second = executor.submit(
            _inspect_stats.cached_live_lifecycle,
            7,
            window=window,
            freeze=now,
        )
        release.set()
        assert first.result(timeout=2) == second.result(timeout=2)
    assert calls == 1


def test_inspect_pre_cutover_agent_uses_indexed_lifecycle_window(
    db_conn: psycopg.Connection,
    fake_loki: _FakeLoki,
) -> None:
    """The live lifecycle leg skips legacy rows without losing open alive-time."""
    aid = _insert_agent(db_conn)
    with db_conn.cursor() as cur:
        cur.execute(
            "UPDATE agents_meta SET spawned_at = %s WHERE id = %s",
            (INDEX_LABEL_CUTOVER_AT - timedelta(hours=12), aid),
        )
    db_conn.commit()

    with TestClient(app) as client:
        response = client.get(f"/api/agents/{aid}/inspect", params={"hours": 24})

    assert response.status_code == 200
    lifecycle_calls = _lifecycle_projected_calls(fake_loki)
    assert len(lifecycle_calls) == 1
    assert lifecycle_calls[0]["from_"] == INDEX_LABEL_CUTOVER_AT
    alive_seconds = response.json()["activity"]["alive_seconds"]
    assert alive_seconds == pytest.approx(24 * 60 * 60, abs=30)  # pyright: ignore[reportUnknownMemberType]


def test_inspect_lifecycle_window_is_clipped_by_archive_freeze(
    db_conn: psycopg.Connection,
    fake_loki: _FakeLoki,
) -> None:
    """A post-cutover archive boundary remains the live lifecycle lower bound."""
    aid = _insert_agent(db_conn)
    freeze = datetime.now(UTC) - timedelta(minutes=30)
    _archive_event(
        db_conn,
        agent_id=aid,
        event="freeze_marker",
        attributes={},
        ts=freeze,
    )
    db_conn.commit()

    with TestClient(app) as client:
        response = client.get(f"/api/agents/{aid}/inspect")

    assert response.status_code == 200
    lifecycle_calls = _lifecycle_projected_calls(fake_loki)
    assert len(lifecycle_calls) == 1
    assert lifecycle_calls[0]["from_"] == freeze


def _ledger_row(
    db: psycopg.Connection,
    *,
    agent_id: int,
    days_ago: int,
    model: str = "claude-opus-4-8",
    calls: int = 1,
    tin: int = 0,
    tout: int = 0,
    tcached: int = 0,
    treason: int = 0,
    cost: float = 0.0,
    costed: int | None = None,
    unpriced: int = 0,
) -> None:
    """INSERT one agent_model_tokens_daily ledger row `days_ago` UTC days back
    (the durable whole-life cost store the maintenance rollup writes)."""
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO agent_model_tokens_daily (agent_id, day, model, llm_calls, "
            "tokens_in, tokens_out, tokens_cached, tokens_reasoning, cost_usd, "
            "costed_calls, unpriced_calls) VALUES "
            "(%s, (now() AT TIME ZONE 'UTC')::date - %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (
                agent_id,
                days_ago,
                model,
                calls,
                tin,
                tout,
                tcached,
                treason,
                cost,
                calls if costed is None else costed,
                unpriced,
            ),
        )


def _metrics_ledger_row(
    db: psycopg.Connection,
    *,
    agent_id: int,
    days_ago: int,
    turn_total: int,
    turn_ok: int,
    turn_duration_seconds: float,
    exec_ok: int = 0,
    exec_failed: int = 0,
) -> None:
    """Insert one completed UTC day for PG-backed inspector stats."""
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO agent_metrics_daily "
            "(agent_id, day, turn_total, turn_ok, turn_dur_sum, turn_dur_min, turn_dur_max, exec_ok, exec_failed) "
            "VALUES (%s, (now() AT TIME ZONE 'UTC')::date - %s, %s, %s, %s, %s, %s, %s, %s)",
            (
                agent_id,
                days_ago,
                turn_total,
                turn_ok,
                turn_duration_seconds,
                turn_duration_seconds,
                turn_duration_seconds,
                exec_ok,
                exec_failed,
            ),
        )


def _archive_event(
    db: psycopg.Connection,
    *,
    agent_id: int,
    event: str,
    attributes: dict[str, Any],
    ts: datetime,
) -> None:
    """Insert one frozen event row; the later marker establishes its cutoff."""
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO events "
            "(ts, agent_id, machine, process, category, event_name, level, source, attributes) "
            "VALUES (%s, %s, 'test', 'test', 'telemetry', %s, 'info', 'test', %s::jsonb)",
            (ts, agent_id, event, json.dumps(attributes)),
        )


def _insert_pending_inbound(
    db: psycopg.Connection, *, agent_id: int, kind: str = "heartbeat"
) -> None:
    """INSERT a pending inbound_messages row — models a check-in (or any wake) the
    daemon has queued but the agent has not yet claimed. `status` defaults to
    'pending', so this is what the daemon's `NOT EXISTS (pending inbound)` guard
    (and now the inspector's `heartbeat_pending`) keys off."""
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO inbound_messages (agent_id, content, kind) VALUES (%s, %s, %s)",
            (agent_id, "Heartbeat.", kind),
        )


def test_inspect_unknown_agent_404(db_conn: psycopg.Connection) -> None:
    """No agents_meta row → 404 (fail-fast, no empty shell)."""
    db_conn.commit()
    with TestClient(app) as client:
        resp = client.get("/api/agents/999999/inspect")
    assert resp.status_code == 404


def test_inspect_live_returns_only_window_independent_fields(
    db_conn: psycopg.Connection,
    fake_loki: _FakeLoki,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The live route is the cheap inspector skeleton, not the aggregate payload."""
    aid = _insert_agent(
        db_conn,
        status="idling",
        config_overlay={"llm_model": "claude-opus-4-8"},
    )
    with db_conn.cursor() as cur:
        cur.execute("UPDATE agents_meta SET machine = 'wsl' WHERE id = %s", (aid,))
    fake_loki.add(
        event="heartbeat_paused",
        agent_id=aid,
        payload={"duration_s": 1800},
        ts_offset_hours=1,
    )
    db_conn.commit()

    async def dispatch(
        target_machine: str, kind: str, payload: dict[str, object], **kwargs: object
    ) -> dict[str, object]:
        assert (target_machine, kind, payload) == (
            "wsl",
            "shell_probe",
            {"agent_id": aid},
        )
        return {
            "shells": [{"id": 5, "name": "live-shell", "created_at": None, "uptime_seconds": 42}]
        }

    monkeypatch.setattr(agent_inspect._cluster_rpc, "dispatch_to_machine", dispatch)
    with TestClient(app) as client:
        response = client.get(f"/api/agents/{aid}/inspect/live")

    assert response.status_code == 200
    body = response.json()
    assert set(body) == {
        "agent_id",
        "machine",
        "liveness_state",
        "last_probe_at",
        "spawned_at",
        "started_at",
        "shells",
        "config_overlay",
        "notice",
        "heartbeat",
    }
    assert body["agent_id"] == aid
    assert body["machine"] == "wsl"
    assert body["config_overlay"] == {"llm_model": "claude-opus-4-8"}
    assert body["shells"] == [
        {"id": 5, "name": "live-shell", "created_at": None, "uptime_seconds": 42}
    ]
    assert body["heartbeat"]["last_pause"]["duration_s"] == 1800
    assert {"cost", "stats", "tps", "activity"}.isdisjoint(body)


def test_inspect_live_unknown_agent_404(db_conn: psycopg.Connection) -> None:
    db_conn.commit()
    with TestClient(app) as client:
        response = client.get("/api/agents/999999/inspect/live")
    assert response.status_code == 404


def test_inspect_live_probe_failure_degrades_to_empty_shells(
    db_conn: psycopg.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    aid = _insert_agent(db_conn)
    db_conn.commit()

    async def unreachable(*args: object, **kwargs: object) -> dict[str, object]:
        raise agent_inspect._cluster_rpc.ClusterOpUnreachable("connect failed")

    monkeypatch.setattr(agent_inspect._cluster_rpc, "dispatch_to_machine", unreachable)
    with TestClient(app) as client:
        response = client.get(f"/api/agents/{aid}/inspect/live")
    assert response.status_code == 200
    assert response.json()["shells"] == []


@pytest.mark.parametrize(
    "error",
    [httpx.RemoteProtocolError("Loki closed the response"), ValueError("invalid Loki JSON")],
)
def test_inspect_live_loki_failure_degrades_last_pause_to_none(
    db_conn: psycopg.Connection,
    monkeypatch: pytest.MonkeyPatch,
    error: Exception,
) -> None:
    aid = _insert_agent(db_conn, status="idling")
    db_conn.commit()

    def unavailable(_agent_id: int) -> None:
        raise error

    monkeypatch.setattr(agent_inspect, "_heartbeat_last_pause", unavailable)
    with TestClient(app) as client:
        response = client.get(f"/api/agents/{aid}/inspect/live")
    assert response.status_code == 200
    assert response.json()["heartbeat"]["last_pause"] is None


@pytest.mark.parametrize("reason", ["queue_full", "acquire_timeout"])
def test_inspect_local_loki_budget_rejection_is_503(
    db_conn: psycopg.Connection,
    monkeypatch: pytest.MonkeyPatch,
    reason: Literal["queue_full", "acquire_timeout"],
) -> None:
    """Local capacity saturation is retriable, never an unhandled 500."""
    aid = _insert_agent(db_conn)
    db_conn.commit()

    async def reject(*args: Any, **kwargs: Any) -> Any:
        raise loki_query_budget.LokiQueryBudgetError(reason)

    monkeypatch.setattr(agent_inspect, "_inspect_rows_cached_async", reject)
    with TestClient(app) as client:
        response = client.get(f"/api/agents/{aid}/inspect")
    assert response.status_code == 503
    assert response.json()["detail"] == f"Loki query budget unavailable ({reason}); retry"
    assert response.headers["retry-after"] == "1"


def test_inspect_loki_transport_failure_is_retriable_503(
    db_conn: psycopg.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A dropped Loki response is a retriable dependency failure, not a 500."""
    aid = _insert_agent(db_conn)
    db_conn.commit()

    async def reject(*args: Any, **kwargs: Any) -> Any:
        raise httpx.RemoteProtocolError("Loki closed the response")

    monkeypatch.setattr(agent_inspect, "_inspect_rows_cached_async", reject)
    with TestClient(app) as client:
        response = client.get(f"/api/agents/{aid}/inspect")
    assert response.status_code == 503
    assert "loki backend unavailable (RemoteProtocolError)" in response.json()["detail"]
    assert response.headers["retry-after"] == "1"


def test_inspect_total_deadline_returns_503_then_cached_loader_can_finish(
    db_conn: psycopg.Connection,
    fake_loki: _FakeLoki,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A cold aggregate miss has a response deadline without bricking its cache key.

    The synchronous leader is deliberately allowed to finish after the caller
    receives 503: cancelling a Python worker thread is unsafe. Single-flight
    keeps later callers from starting duplicate Loki fan-outs, and the completed
    value becomes the normal TTL hit.
    """
    aid = _insert_agent(db_conn)
    fake_loki.add(event="turn_end", agent_id=aid, payload={"duration_seconds": 1.0, "ok": True})
    db_conn.commit()
    agent_inspect.cache_clear()
    original = agent_inspect._inspect_blocking
    loader_finished = threading.Event()

    def delayed_loader(*args: Any, **kwargs: Any) -> Any:
        time.sleep(0.05)
        try:
            return original(*args, **kwargs)
        finally:
            loader_finished.set()

    monkeypatch.setattr(agent_inspect, "_INSPECT_RESPONSE_TIMEOUT_S", 0.01)
    monkeypatch.setattr(agent_inspect, "_inspect_blocking", delayed_loader)
    with TestClient(app) as client:
        timed_out = client.get(f"/api/agents/{aid}/inspect")
        assert timed_out.status_code == 503
        assert timed_out.json()["detail"] == "inspector history query timed out; retry"
        assert timed_out.headers["retry-after"] == "1"
        assert loader_finished.wait(timeout=1)
        cached = client.get(f"/api/agents/{aid}/inspect")

    assert cached.status_code == 200
    assert cached.json()["stats"]["turn_total"] == 1


def test_inspect_config_overlay_roundtrips(db_conn: psycopg.Connection) -> None:
    """config_overlay JSONB pass-through as-is; shells is a list, machine echoed."""
    aid = _insert_agent(
        db_conn,
        config_overlay={"llm_model": "claude-opus-4-8", "auto_compact_fraction": 0.7},
    )
    db_conn.commit()
    with TestClient(app) as client:
        body = client.get(f"/api/agents/{aid}/inspect").json()
    assert body["agent_id"] == aid
    assert body["config_overlay"] == {
        "llm_model": "claude-opus-4-8",
        "auto_compact_fraction": 0.7,
    }
    assert isinstance(body["shells"], list)


def test_inspect_null_config_overlay_is_empty_dict(
    db_conn: psycopg.Connection, fake_loki: _FakeLoki
) -> None:
    """config_overlay NULL (cluster defaults) → {} not null."""
    aid = _insert_agent(db_conn, config_overlay=None)
    db_conn.commit()
    with TestClient(app) as client:
        body = client.get(f"/api/agents/{aid}/inspect").json()
    assert body["config_overlay"] == {}


# ── Shells section (shell_probe op, uniform machine path) ─────────────────────


def test_inspect_shells_probed_on_agents_machine(
    db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """shells come from a `shell_probe` op dispatched to the agent's machine —
    a remote runner's live shells appear exactly like a local one's (no local
    session probing in the gateway)."""
    from gateway.routers import agent_inspect as inspect_mod

    aid = _insert_agent(db_conn)
    with db_conn.cursor() as cur:
        cur.execute("UPDATE agents_meta SET machine = 'wsl' WHERE id = %s", (aid,))
    db_conn.commit()

    seen: dict[str, object] = {}

    async def _fake_dispatch(
        target_machine: str, kind: str, payload: dict[str, object], **kwargs: object
    ) -> dict[str, object]:
        seen["machine"] = target_machine
        seen["kind"] = kind
        seen["payload"] = payload
        return {
            "shells": [
                {"id": 5, "name": "desktop-remove", "created_at": None, "uptime_seconds": 42},
            ]
        }

    monkeypatch.setattr(inspect_mod._cluster_rpc, "dispatch_to_machine", _fake_dispatch)

    with TestClient(app) as client:
        body = client.get(f"/api/agents/{aid}/inspect").json()
    assert seen == {
        "machine": "wsl",
        "kind": "shell_probe",
        "payload": {"agent_id": aid},
    }
    assert body["shells"] == [
        {"id": 5, "name": "desktop-remove", "created_at": None, "uptime_seconds": 42}
    ]


def test_inspect_shells_degrade_to_empty_on_unreachable(
    db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unreachable machine (or unregistered name) degrades to an empty shell
    list — the inspector shows 'None open' instead of 503ing the whole panel."""
    from gateway.routers import agent_inspect as inspect_mod
    from ops import cluster_rpc

    aid = _insert_agent(db_conn)
    with db_conn.cursor() as cur:
        cur.execute("UPDATE agents_meta SET machine = 'wsl' WHERE id = %s", (aid,))
    db_conn.commit()

    async def _unreachable_dispatch(
        target_machine: str, kind: str, payload: dict[str, object], **kwargs: object
    ) -> dict[str, object]:
        raise cluster_rpc.ClusterOpUnreachable("connect failed")

    monkeypatch.setattr(inspect_mod._cluster_rpc, "dispatch_to_machine", _unreachable_dispatch)

    with TestClient(app) as client:
        resp = client.get(f"/api/agents/{aid}/inspect")
    assert resp.status_code == 200
    assert resp.json()["shells"] == []


def test_inspect_shells_degrade_to_empty_on_failed_op(
    db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A version-skewed runner that does not know the op reports 'failed' —
    same graceful degradation to an empty shell list."""
    from gateway.routers import agent_inspect as inspect_mod
    from ops import cluster_rpc

    aid = _insert_agent(db_conn)
    with db_conn.cursor() as cur:
        cur.execute("UPDATE agents_meta SET machine = 'wsl' WHERE id = %s", (aid,))
    db_conn.commit()

    async def _failed_dispatch(
        target_machine: str, kind: str, payload: dict[str, object], **kwargs: object
    ) -> dict[str, object]:
        raise cluster_rpc.ClusterOpFailed({"error": "unknown kind: shell_probe"})

    monkeypatch.setattr(inspect_mod._cluster_rpc, "dispatch_to_machine", _failed_dispatch)

    with TestClient(app) as client:
        resp = client.get(f"/api/agents/{aid}/inspect")
    assert resp.status_code == 200
    assert resp.json()["shells"] == []


def test_inspect_audit_rows_excluded_from_cost_and_stats(
    db_conn: psycopg.Connection,
    fake_loki: _FakeLoki,
) -> None:
    """The inspector's cost/stats queries filter category IN (telemetry, log)
    (matching their docstring) — an audit row shaped like llm_usage / turn_end
    (e.g. a mislabeled write) must not inflate the per-agent aggregates
    (appendix scenario 9)."""
    aid = _insert_agent(db_conn)
    fake_loki.add(
        event="llm_usage",
        agent_id=aid,
        payload={"in_total": 1000, "out_total": 500, "cache_read": 0, "model": "m"},
        category="audit",
    )
    fake_loki.add(
        event="turn_end",
        agent_id=aid,
        payload={"duration_seconds": 99.0, "ok": True},
        category="audit",
    )
    fake_loki.add(
        event="exec",
        agent_id=aid,
        payload={},
        category="audit",
    )
    db_conn.commit()
    with TestClient(app) as client:
        body = client.get(f"/api/agents/{aid}/inspect").json()
    assert body["cost"]["cost_usd"] == 0
    assert body["cost"]["llm_calls"] == 0
    assert body["cost"]["tokens_in"] == 0
    assert body["stats"]["turn_total"] == 0
    assert body["stats"]["exec_ok"] == 0
    assert body["stats"]["exec_failed"] == 0


def test_inspect_cost_is_cumulative_no_window(
    db_conn: psycopg.Connection, fake_loki: _FakeLoki
) -> None:
    """Whole-life cost reads the daily ledger — a rolled day far older than
    Loki retention still counts (no time window), contrasting with the
    dashboard's 24h window. Lock field names: tokens_in/out/reasoning."""
    aid = _insert_agent(db_conn)
    _ledger_row(
        db_conn,
        agent_id=aid,
        days_ago=10,
        tin=1_000_000,
        tout=1_000_000,
        treason=12345,
        cost=30.0,
    )
    db_conn.commit()
    with TestClient(app) as client:
        body = client.get(f"/api/agents/{aid}/inspect").json()
    cost = body["cost"]
    assert cost["cost_usd"] == pytest.approx(30.0)  # pyright: ignore[reportUnknownMemberType]
    assert cost["llm_calls"] == 1
    assert cost["tokens_in"] == 1_000_000
    assert cost["tokens_out"] == 1_000_000
    assert cost["tokens_reasoning"] == 12345
    assert cost["unpriced_calls"] == 0


def test_inspect_whole_life_tail_bounded_to_retention(
    db_conn: psycopg.Connection, fake_loki: _FakeLoki
) -> None:
    """With no rolled ledger day, the whole-life Loki tail starts at the
    retention floor (now − 168h): a Loki row older than retention (which
    real Loki would not even hold) falls outside; a recent snapshot row
    counts. No far-past sentinel, no max_query_length 400 (the 2026-08-12
    prod inspector 500)."""
    aid = _insert_agent(db_conn)
    # 40d-old llm_usage — beyond Loki's reach; must not be counted
    fake_loki.add(
        event="llm_usage",
        agent_id=aid,
        payload={
            "in_total": 1_000_000,
            "out_total": 1_000_000,
            "cache_read": 0,
            "model": "claude-opus-4-8",
        },
        ts_offset_hours=40 * 24,
    )
    fake_loki.add(
        event="llm_usage",
        agent_id=aid,
        payload={
            "in_total": 1_000_000,
            "out_total": 1_000_000,
            "cache_read": 0,
            "model": "claude-opus-4-8",
            "cost_usd": 30.0,
        },
        ts_offset_hours=1,
    )
    db_conn.commit()
    with TestClient(app) as client:
        body = client.get(f"/api/agents/{aid}/inspect").json()
    assert body["cost"]["llm_calls"] == 1
    assert body["cost"]["cost_usd"] == pytest.approx(30.0)  # pyright: ignore[reportUnknownMemberType]


def test_inspect_cost_unpriced_and_cache_hit(
    db_conn: psycopg.Connection, fake_loki: _FakeLoki
) -> None:
    """Unpriced model → cost 0 but counted as unpriced_calls; cache_hit_pct = cache/in."""
    aid = _insert_agent(db_conn)
    fake_loki.add(
        event="llm_usage",
        agent_id=aid,
        payload={"in_total": 1000, "out_total": 100, "cache_read": 800, "model": "no-such-model"},
    )
    db_conn.commit()
    with TestClient(app) as client:
        body = client.get(f"/api/agents/{aid}/inspect").json()
    cost = body["cost"]
    assert cost["cost_usd"] == 0
    assert cost["unpriced_calls"] == 1
    assert cost["llm_calls"] == 1
    # 800 / 1000 = 80%
    assert cost["cache_hit_pct"] == 80.0


def test_inspect_cost_aggregates_share_one_snapshot_instant(
    db_conn: psycopg.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Each cost fan-out pins its seven Loki aggregates to one instant."""
    aid = _insert_agent(db_conn)
    recorded_to: list[datetime | None] = []
    aggregate: Callable[..., Any] = loki_events.attribute_aggregate

    def record_snapshot_instant(**kwargs: Any) -> Any:
        recorded_to.append(kwargs["to"])
        return aggregate(**kwargs)

    monkeypatch.setattr(loki_events, "attribute_aggregate", record_snapshot_instant)
    with TestClient(app):
        pool = app.state.db_pool
        for hours in (None, _agent_cost.StatsWindowHours.H24):
            _agent_cost.cache_clear()
            _agent_cost.agent_cost(pool, aid, hours, since_compact=False)
            assert len(recorded_to) == 7
            assert recorded_to[0] is not None
            assert all(to == recorded_to[0] for to in recorded_to)
            recorded_to.clear()


def test_inspect_cost_report_clamps_invalid_aggregates() -> None:
    """Out-of-domain aggregates cannot invalidate the complete inspector report."""
    agg = _agent_cost._ModelAgg()
    agg.tin = 100
    agg.tcached = 101
    agg.unpriced_calls = -1

    cost = _agent_cost._to_agent_cost({"test": agg})

    assert cost.unpriced_calls == 0
    assert cost.cache_hit_pct == 100.0


def test_inspect_cost_scoped_to_agent(db_conn: psycopg.Connection, fake_loki: _FakeLoki) -> None:
    """Another agent's llm_usage does not pollute this agent's cost."""
    aid = _insert_agent(db_conn)
    other = _insert_agent(db_conn)
    fake_loki.add(
        event="llm_usage",
        agent_id=other,
        payload={
            "in_total": 5_000_000,
            "out_total": 5_000_000,
            "cache_read": 0,
            "model": "claude-opus-4-7",
        },
    )
    db_conn.commit()
    with TestClient(app) as client:
        body = client.get(f"/api/agents/{aid}/inspect").json()
    assert body["cost"]["cost_usd"] == 0
    assert body["cost"]["llm_calls"] == 0


def test_inspect_whole_life_ledger_plus_tail_no_double_count(
    db_conn: psycopg.Connection, fake_loki: _FakeLoki
) -> None:
    """Task #1273 regression class: whole-life cost must not collapse to the
    Loki window (405: ~$14.5 history shown as $0.74). The daily ledger
    serves history — including days beyond Loki's reach — while the stale
    gap-day row is excluded and its events are reread from the live tail."""
    aid = _insert_agent(db_conn)
    _ledger_row(db_conn, agent_id=aid, days_ago=40, tin=1_000_000, tout=1_000_000, cost=30.0)
    _ledger_row(db_conn, agent_id=aid, days_ago=3, tin=500_000, tout=500_000, cost=15.0)
    _ledger_row(db_conn, agent_id=aid, days_ago=2, tin=500_000, tout=500_000, cost=15.0)
    # The gap day's live event replaces its stale rolled row exactly once.
    fake_loki.add(
        event="llm_usage",
        agent_id=aid,
        payload={
            "in_total": 500_000,
            "out_total": 500_000,
            "cache_read": 0,
            "model": "claude-opus-4-8",
            "cost_usd": 15.0,
        },
        ts_offset_hours=2 * 24,
    )
    db_conn.commit()
    with TestClient(app) as client:
        body = client.get(f"/api/agents/{aid}/inspect").json()
    cost = body["cost"]
    assert cost["llm_calls"] == 3
    assert cost["tokens_in"] == 2_000_000
    assert cost["cost_usd"] == pytest.approx(60.0)  # pyright: ignore[reportUnknownMemberType]
    assert cost["unpriced_calls"] == 0


def test_inspect_whole_life_gap_day_cost_not_lost_and_not_double_counted(
    db_conn: psycopg.Connection, fake_loki: _FakeLoki
) -> None:
    """The gap-day tail replaces a stale cost row, including its final hour."""
    aid = _insert_agent(db_conn)
    now = datetime.now(UTC)
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    _ledger_row(
        db_conn,
        agent_id=aid,
        days_ago=1,
        calls=5,
        tin=1_000_000,
        tout=1_000_000,
        cost=10.0,
    )
    fake_loki.add(
        event="llm_usage",
        agent_id=aid,
        payload={
            "in_total": 200_000,
            "out_total": 40_000,
            "cache_read": 0,
            "model": "claude-opus-4-8",
            "cost_usd": 2.0,
        },
        ts=today - timedelta(minutes=30),
    )
    fake_loki.add(
        event="llm_usage",
        agent_id=aid,
        payload={
            "in_total": 300_000,
            "out_total": 50_000,
            "cache_read": 0,
            "model": "claude-opus-4-8",
            "cost_usd": 3.0,
        },
        ts=now - timedelta(seconds=30),
    )
    db_conn.commit()
    with TestClient(app) as client:
        cost = client.get(f"/api/agents/{aid}/inspect").json()["cost"]
    assert cost["cost_usd"] == pytest.approx(5.0)  # pyright: ignore[reportUnknownMemberType]
    assert cost["llm_calls"] == 2
    assert cost["tokens_in"] == 500_000
    assert cost["unpriced_calls"] == 0


def test_inspect_whole_life_gap_day_tokens_reread_the_closed_day(
    db_conn: psycopg.Connection, fake_loki: _FakeLoki
) -> None:
    """A late 23:50 usage row joins the newest day's live token reread once."""
    aid = _insert_agent(db_conn)
    now = datetime.now(UTC)
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    _ledger_row(db_conn, agent_id=aid, days_ago=1, tout=100)
    # The ledger row is stale; the live reread has the original 100-token row
    # and the late 50-token write at 23:50 of the newest closed day.
    fake_loki.add(
        event="llm_usage",
        agent_id=aid,
        payload={"out_total": 100, "model": "claude-opus-4-8"},
        ts=today - timedelta(hours=12),
    )
    fake_loki.add(
        event="llm_usage",
        agent_id=aid,
        payload={"out_total": 50, "model": "claude-opus-4-8"},
        ts=today - timedelta(minutes=10),
    )
    fake_loki.add(
        event="turn_end",
        agent_id=aid,
        payload={"duration_seconds": 1.0, "ok": True},
        ts=today - timedelta(minutes=20),
    )
    db_conn.commit()

    with TestClient(app) as client:
        response = client.get(f"/api/agents/{aid}/inspect")

    assert response.status_code == 200
    # lm_stage_tps is output_tokens / turn duration, so the one-second turn
    # exposes the private token aggregate as exactly X + Y.
    assert response.json()["tps"]["lm_stage_tps"] == 150.0


def test_inspect_snapshot_cost_immune_to_registry_changes(
    db_conn: psycopg.Connection, fake_loki: _FakeLoki
) -> None:
    """User principle (task #1273): cost is billed at usage time — a row's
    stored cost_usd is summed as-is and NEVER re-priced against the current
    registry. Both rows carry snapshots whose values differ from what the
    registry would compute today; the response must equal the snapshots, not
    the registry math. The newest retained row is instead reread live."""
    aid = _insert_agent(db_conn)
    _ledger_row(
        db_conn,
        agent_id=aid,
        days_ago=3,
        model="deepseek-v4-pro",
        tin=1_000_000,
        tout=1_000_000,
        cost=50.0,
    )
    _ledger_row(
        db_conn,
        agent_id=aid,
        days_ago=2,
        model="deepseek-v4-pro",
        tin=1_000_000,
        tout=1_000_000,
        cost=1.0,
    )
    fake_loki.add(
        event="llm_usage",
        agent_id=aid,
        payload={
            "in_total": 1_000_000,
            "out_total": 1_000_000,
            "cache_read": 0,
            "model": "deepseek-v4-pro",
            "cost_usd": 99.0,
        },
    )
    db_conn.commit()
    with TestClient(app) as client:
        body = client.get(f"/api/agents/{aid}/inspect").json()
    cost = body["cost"]
    assert cost["cost_usd"] == pytest.approx(149.0)  # pyright: ignore[reportUnknownMemberType]
    # tokens still aggregate normally alongside the snapshotted cost
    assert cost["tokens_in"] == 2_000_000
    assert cost["unpriced_calls"] == 0
    assert cost["llm_calls"] == 2


def test_inspect_snapshotless_rows_are_unpriced(
    db_conn: psycopg.Connection, fake_loki: _FakeLoki
) -> None:
    """A row without a stored cost snapshot contributes 0 and counts in
    unpriced_calls — there is no read-time re-pricing (cost is billed at
    usage time or not at all). Mixed model: the snapshot row's cost is the
    whole figure; both rows count as calls."""
    aid = _insert_agent(db_conn)
    fake_loki.add(
        event="llm_usage",
        agent_id=aid,
        payload={
            "in_total": 1_000_000,
            "out_total": 0,
            "cache_read": 1_000_000,
            "model": "gpt-5.6-sol",
            "cost_usd": 0.5,
        },
        ts_offset_hours=1,
    )
    fake_loki.add(
        event="llm_usage",
        agent_id=aid,
        payload={
            "in_total": 1_000_000,
            "out_total": 0,
            "cache_read": 1_000_000,
            "model": "gpt-5.6-sol",
        },
    )
    db_conn.commit()
    with TestClient(app) as client:
        body = client.get(f"/api/agents/{aid}/inspect").json()
    cost = body["cost"]
    assert cost["llm_calls"] == 2
    assert cost["unpriced_calls"] == 1
    assert cost["cost_usd"] == pytest.approx(0.5)  # pyright: ignore[reportUnknownMemberType]


def test_inspect_no_read_time_pricing_even_for_known_models(
    db_conn: psycopg.Connection, fake_loki: _FakeLoki
) -> None:
    """The pricing registry is never consulted at read time: a snapshot-less
    row of a model the registry DOES know still lands as unpriced with 0
    cost. (Usage-time billing only — the emitter stamps the snapshot or the
    unpriced marker at the call; the read side just sums.)"""
    aid = _insert_agent(db_conn)
    fake_loki.add(
        event="llm_usage",
        agent_id=aid,
        payload={
            "in_total": 1_000_000,
            "out_total": 1_000_000,
            "cache_read": 0,
            "model": "claude-opus-4-8",
        },
    )
    db_conn.commit()
    with TestClient(app) as client:
        body = client.get(f"/api/agents/{aid}/inspect").json()
    cost = body["cost"]
    assert cost["cost_usd"] == 0
    assert cost["unpriced_calls"] == 1
    assert cost["llm_calls"] == 1


def test_inspect_hours_window_is_loki_only(
    db_conn: psycopg.Connection, fake_loki: _FakeLoki
) -> None:
    """An hours window (StatsWindowHours caps at 168h = Loki retention)
    aggregates pure Loki: rolled ledger days do NOT leak into it, the
    window scopes by event ts, and whole-life still sees both stores."""
    aid = _insert_agent(db_conn)
    _ledger_row(db_conn, agent_id=aid, days_ago=20, tin=1_000_000, cost=30.0)
    fake_loki.add(
        event="llm_usage",
        agent_id=aid,
        payload={
            "in_total": 1_000_000,
            "out_total": 1_000_000,
            "cache_read": 0,
            "model": "claude-opus-4-8",
            "cost_usd": 2.0,
        },
        ts_offset_hours=100,
    )
    db_conn.commit()
    with TestClient(app) as client:
        body = client.get(f"/api/agents/{aid}/inspect?hours=168").json()
        body24 = client.get(f"/api/agents/{aid}/inspect?hours=24").json()
        whole = client.get(f"/api/agents/{aid}/inspect").json()
    cost = body["cost"]
    assert cost["llm_calls"] == 1
    assert cost["cost_usd"] == pytest.approx(2.0)  # pyright: ignore[reportUnknownMemberType]
    # 24h window: the 100h-old row is outside
    assert body24["cost"]["llm_calls"] == 0
    # whole life: ledger day + Loki tail
    assert whole["cost"]["llm_calls"] == 2
    assert whole["cost"]["cost_usd"] == pytest.approx(32.0)  # pyright: ignore[reportUnknownMemberType]


def test_inspect_turn_stats(db_conn: psycopg.Connection, fake_loki: _FakeLoki) -> None:
    """turn_total/turn_ok + duration p50 (percentile_cont). ok=False still counted in total,
    but not in turn_ok."""
    aid = _insert_agent(db_conn)
    for dur in (2.0, 4.0, 6.0):
        fake_loki.add(
            event="turn_end",
            agent_id=aid,
            payload={"duration_seconds": dur, "ok": True},
        )
    fake_loki.add(
        event="turn_end",
        agent_id=aid,
        payload={"duration_seconds": 100.0, "ok": False},
    )
    db_conn.commit()
    with TestClient(app) as client:
        body = client.get(f"/api/agents/{aid}/inspect").json()
    stats = body["stats"]
    assert stats["turn_total"] == 4
    assert stats["turn_ok"] == 3
    # percentile_cont(0.5) over [2,4,6,100] = 5.0; p90 = 71.8; min=2.0; max=100.0
    assert stats["turn_p50_seconds"] == 5.0
    assert stats["turn_p90_seconds"] == 71.8
    assert stats["turn_min_seconds"] == 2.0
    assert stats["turn_max_seconds"] == 100.0


def test_inspect_windowed_duration_stats_keep_exact_float_values(
    db_conn: psycopg.Connection, fake_loki: _FakeLoki
) -> None:
    """A single live source keeps fractional durations exact (Task #1394)."""
    aid = _insert_agent(db_conn)
    for duration in (2.48, 2.48, 96.04):
        fake_loki.add(
            event="turn_end",
            agent_id=aid,
            payload={"duration_seconds": duration, "ok": True},
        )
    db_conn.commit()
    with TestClient(app) as client:
        stats = client.get(f"/api/agents/{aid}/inspect?hours=24").json()["stats"]
    assert stats["turn_p50_seconds"] == 2.48
    assert stats["turn_p90_seconds"] == 77.33
    assert stats["turn_min_seconds"] == 2.48
    assert stats["turn_max_seconds"] == 96.04


def test_inspect_archive_and_live_durations_keep_exact_percentiles(
    db_conn: psycopg.Connection, fake_loki: _FakeLoki
) -> None:
    """The archive/live seam preserves fractions instead of second buckets."""
    aid = _insert_agent(db_conn)
    now = datetime.now(UTC)
    _archive_event(
        db_conn,
        agent_id=aid,
        event="turn_end",
        attributes={"duration_seconds": 1.25, "ok": True},
        ts=now - timedelta(hours=2),
    )
    # The archive cutoff is exclusive, so this marker leaves the turn archived
    # and starts the live window before the FakeLoki row below.
    _archive_event(
        db_conn,
        agent_id=aid,
        event="freeze_marker",
        attributes={},
        ts=now - timedelta(hours=1),
    )
    fake_loki.add(
        event="turn_end",
        agent_id=aid,
        payload={"duration_seconds": 1.75, "ok": True},
        ts=now - timedelta(minutes=30),
    )
    db_conn.commit()

    with TestClient(app) as client:
        stats = client.get(f"/api/agents/{aid}/inspect").json()["stats"]

    assert stats["turn_p50_seconds"] == 1.5
    assert stats["turn_p90_seconds"] == 1.7


def _assert_inspect_live_query_budget(fake_loki: _FakeLoki) -> None:
    """The cold panel keeps cost/heartbeat and collapses every other live read."""
    assert sum(fake_loki.wire_calls.values()) <= 12
    assert fake_loki.wire_calls["attribute_aggregate"] == 7
    assert fake_loki.wire_calls["query_events"] == 1
    # Requested stats and the retained per-agent lifecycle leg are separate:
    # the latter is cached for thirty minutes across inspector windows.
    assert fake_loki.wire_calls["query_projected_lines"] == 2
    assert fake_loki.wire_calls["count_by_event_name"] == 0
    assert fake_loki.wire_calls["attribute_distribution"] == 0


def test_inspect_cold_panel_with_ledger_uses_one_shared_live_pass(
    db_conn: psycopg.Connection, fake_loki: _FakeLoki
) -> None:
    """A ledger-backed whole-life panel needs cost, heartbeat, and one live pass."""
    aid = _insert_agent(db_conn)
    _ledger_row(db_conn, agent_id=aid, days_ago=2, tout=100)
    _metrics_ledger_row(
        db_conn,
        agent_id=aid,
        days_ago=2,
        turn_total=1,
        turn_ok=1,
        turn_duration_seconds=2.0,
    )
    fake_loki.add(event="agent_spawned", agent_id=aid, ts_offset_hours=2)
    fake_loki.add(event="turn_end", agent_id=aid, payload={"duration_seconds": 2.5, "ok": True})
    fake_loki.add(event="llm_usage", agent_id=aid, payload={"out_total": 25})
    _node_exit(fake_loki, agent_id=aid, node="exec", duration_seconds=1.5)
    db_conn.commit()

    fake_loki.wire_calls.clear()
    with TestClient(app) as client:
        assert client.get(f"/api/agents/{aid}/inspect").status_code == 200

    _assert_inspect_live_query_budget(fake_loki)


def test_inspect_cold_all_live_panel_uses_one_shared_live_pass(
    db_conn: psycopg.Connection, fake_loki: _FakeLoki
) -> None:
    """No ledger rows still produce one consolidated raw line fetch."""
    aid = _insert_agent(db_conn)
    fake_loki.add(event="agent_spawned", agent_id=aid, ts_offset_hours=2)
    fake_loki.add(event="turn_end", agent_id=aid, payload={"duration_seconds": 2.5, "ok": True})
    fake_loki.add(event="llm_usage", agent_id=aid, payload={"out_total": 25})
    _node_exit(fake_loki, agent_id=aid, node="exec", duration_seconds=1.5)
    db_conn.commit()

    fake_loki.wire_calls.clear()
    with TestClient(app) as client:
        assert client.get(f"/api/agents/{aid}/inspect").status_code == 200

    _assert_inspect_live_query_budget(fake_loki)


def test_inspect_whole_life_stats_read_completed_days_from_the_ledger(
    db_conn: psycopg.Connection, fake_loki: _FakeLoki
) -> None:
    """The live-covered gap day is reread wholly and its stale row excluded."""
    aid = _insert_agent(db_conn)
    now = datetime.now(UTC)
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    _metrics_ledger_row(
        db_conn,
        agent_id=aid,
        days_ago=2,
        turn_total=7,
        turn_ok=6,
        turn_duration_seconds=3.0,
        exec_ok=4,
        exec_failed=2,
    )
    _metrics_ledger_row(
        db_conn,
        agent_id=aid,
        days_ago=1,
        turn_total=5,
        turn_ok=5,
        turn_duration_seconds=3.0,
        exec_ok=3,
    )
    # Gap-day events include the old midnight-boundary hole; the stale row
    # remains out of the ledger sum while the live tail reads all three rows.
    fake_loki.add(
        event="turn_end",
        agent_id=aid,
        payload={"duration_seconds": 4.0, "ok": True},
        ts=today - timedelta(hours=12) + timedelta(microseconds=1),
    )
    fake_loki.add(
        event="turn_end",
        agent_id=aid,
        payload={"duration_seconds": 4.0, "ok": True},
        ts=today - timedelta(hours=1),
    )
    fake_loki.add(
        event="turn_end",
        agent_id=aid,
        payload={"duration_seconds": 5.0, "ok": True},
        ts=now - timedelta(seconds=30),
    )
    fake_loki.add(event="exec", agent_id=aid, ts=now - timedelta(seconds=30))
    db_conn.commit()
    with TestClient(app) as client:
        response = client.get(f"/api/agents/{aid}/inspect")
    assert response.status_code == 200
    stats = response.json()["stats"]
    assert stats["turn_total"] == 10
    assert stats["turn_ok"] == 9
    assert stats["exec_ok"] == 5
    assert stats["exec_failed"] == 2


def test_inspect_whole_life_gap_day_events_read_live_and_not_double_counted(
    db_conn: psycopg.Connection, fake_loki: _FakeLoki
) -> None:
    """The final gap-day hour is live-read while its stale row stays excluded."""
    aid = _insert_agent(db_conn)
    now = datetime.now(UTC)
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    _metrics_ledger_row(
        db_conn,
        agent_id=aid,
        days_ago=1,
        turn_total=5,
        turn_ok=4,
        turn_duration_seconds=3.0,
        exec_ok=2,
        exec_failed=1,
    )
    fake_loki.add(
        event="turn_end",
        agent_id=aid,
        payload={"duration_seconds": 4.0, "ok": True},
        ts=today - timedelta(minutes=30),
    )
    fake_loki.add(
        event="turn_end",
        agent_id=aid,
        payload={"duration_seconds": 5.0, "ok": True},
        ts=now - timedelta(seconds=30),
    )
    db_conn.commit()
    with TestClient(app) as client:
        stats = client.get(f"/api/agents/{aid}/inspect").json()["stats"]
    assert stats["turn_total"] == 2
    assert stats["turn_ok"] == 2
    assert stats["turn_p50_seconds"] == 4.5
    assert stats["turn_p90_seconds"] == 4.9
    assert stats["turn_min_seconds"] == 4.0
    assert stats["turn_max_seconds"] == 5.0


def test_inspect_exec_ok_fail_split(db_conn: psycopg.Connection, fake_loki: _FakeLoki) -> None:
    """exec_ok = plain 'exec'; exec_failed = exec_failed / exec(timeout) /
    exec_cancelled (the prefix regex also counts legacy exec_thread_stuck rows
    for historical continuity). Non-exec events like 'code' not counted."""
    aid = _insert_agent(db_conn)
    fake_loki.add(event="exec", agent_id=aid)
    fake_loki.add(event="exec", agent_id=aid)
    fake_loki.add(event="exec_failed", agent_id=aid)
    fake_loki.add(event="exec(timeout)", agent_id=aid)
    # non-exec event — must not pollute counts
    fake_loki.add(event="code", agent_id=aid)
    db_conn.commit()
    with TestClient(app) as client:
        body = client.get(f"/api/agents/{aid}/inspect").json()
    stats = body["stats"]
    assert stats["exec_ok"] == 2
    assert stats["exec_failed"] == 2


def test_inspect_projected_rows_deduplicate_repeated_boundary_lines(
    db_conn: psycopg.Connection,
    fake_loki: _FakeLoki,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A projected slice boundary cannot double-count its repeated raw line."""
    aid = _insert_agent(db_conn)
    fake_loki.add(event="turn_end", agent_id=aid, payload={"duration_seconds": 2.0, "ok": True})
    original = fake_loki.query_projected_lines

    def duplicate_boundary_line(**kwargs: Any) -> list[tuple[int, int | None, str]]:
        rows = original(**kwargs)
        return [*rows, *rows]

    monkeypatch.setattr(loki_events, "query_projected_lines", duplicate_boundary_line)
    db_conn.commit()
    with TestClient(app) as client:
        stats = client.get(f"/api/agents/{aid}/inspect").json()["stats"]

    assert stats["turn_total"] == 1
    assert stats["turn_ok"] == 1
    assert stats["turn_min_seconds"] == 2.0
    assert stats["turn_max_seconds"] == 2.0


def test_inspect_empty_agent_zeros(db_conn: psycopg.Connection) -> None:
    """New agent with no events → cost/stats all 0, no error (division by zero degenerate)."""
    aid = _insert_agent(db_conn)
    db_conn.commit()
    with TestClient(app) as client:
        body = client.get(f"/api/agents/{aid}/inspect").json()
    assert body["window_hours"] is None
    assert body["cost"]["cost_usd"] == 0
    assert body["cost"]["cache_hit_pct"] == 0
    assert body["stats"]["turn_total"] == 0
    assert body["stats"]["turn_p50_seconds"] == 0
    assert body["stats"]["exec_ok"] == 0


def test_inspect_hours_windows_cost_and_stats(
    db_conn: psycopg.Connection, fake_loki: _FakeLoki
) -> None:
    """A 24h window is Loki-only; whole life excludes its live-covered D-1 row."""
    aid = _insert_agent(db_conn)
    now = datetime.now(UTC)
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    # Two days ago at noon — deterministically outside the 24h window.
    fake_loki.add(
        event="llm_usage",
        agent_id=aid,
        payload={
            "in_total": 1_000_000,
            "out_total": 1_000_000,
            "cache_read": 0,
            "model": "claude-opus-4-8",
            "cost_usd": 30.0,
        },
        ts=today - timedelta(hours=36),
    )
    fake_loki.add(
        event="turn_end",
        agent_id=aid,
        payload={"duration_seconds": 2.0, "ok": True},
        ts=today - timedelta(hours=36),
    )
    _metrics_ledger_row(
        db_conn,
        agent_id=aid,
        days_ago=2,
        turn_total=1,
        turn_ok=1,
        turn_duration_seconds=2.0,
    )
    _metrics_ledger_row(
        db_conn,
        agent_id=aid,
        days_ago=1,
        turn_total=1,
        turn_ok=1,
        turn_duration_seconds=2.0,
    )
    # Now — always inside the 24h window and after the gap-day tail start.
    fake_loki.add(
        event="llm_usage",
        agent_id=aid,
        payload={
            "in_total": 200_000,
            "out_total": 40_000,
            "cache_read": 0,
            "model": "claude-opus-4-8",
            "cost_usd": 2.0,
        },
        ts=now - timedelta(seconds=30),
    )
    fake_loki.add(
        event="turn_end",
        agent_id=aid,
        payload={"duration_seconds": 4.0, "ok": True},
        ts=now - timedelta(seconds=30),
    )
    db_conn.commit()
    with TestClient(app) as client:
        windowed = client.get(f"/api/agents/{aid}/inspect", params={"hours": 24}).json()
        cumulative = client.get(f"/api/agents/{aid}/inspect").json()
    # windowed: only the 1h row (its stored snapshot, 2.0)
    assert windowed["window_hours"] == 24
    assert windowed["cost"]["llm_calls"] == 1
    assert windowed["cost"]["cost_usd"] == pytest.approx(2.0)  # pyright: ignore[reportUnknownMemberType]
    assert windowed["stats"]["turn_total"] == 1
    # Cumulative: D-2 ledger + live now; the D-1 gap row is not summed.
    assert cumulative["window_hours"] is None
    assert cumulative["cost"]["llm_calls"] == 2
    assert cumulative["cost"]["cost_usd"] == pytest.approx(32.0)  # pyright: ignore[reportUnknownMemberType]
    assert cumulative["stats"]["turn_total"] == 2


def test_inspect_since_compact_windows_cost_and_stats(
    db_conn: psycopg.Connection, fake_loki: _FakeLoki
) -> None:
    """`?since_compact=true` narrows cost + stats to after the last compact halt
    (`ts >=`, the halt row itself is inside the window), and takes precedence over hours —
    even passing hours=1 still counts the 2h old post-compact event. Echoes back since_compact=true + window_hours=None."""
    aid = _insert_agent(db_conn)
    # 5h ago — pre-compact, must be excluded
    fake_loki.add(
        event="llm_usage",
        agent_id=aid,
        payload={
            "in_total": 1_000_000,
            "out_total": 1_000_000,
            "cache_read": 0,
            "model": "claude-opus-4-8",
            "cost_usd": 30.0,
        },
        ts_offset_hours=5,
    )
    fake_loki.add(
        event="turn_end",
        agent_id=aid,
        payload={"duration_seconds": 2.0, "ok": True},
        ts_offset_hours=5,
    )
    # the compact halt at 3h ago is the cutoff
    fake_loki.add(
        event="halt",
        agent_id=aid,
        payload={"body": "system_halt (compact)"},
        ts_offset_hours=3,
    )
    # 2h ago — post-compact but outside hours=1: counted anyway (hours ignored)
    fake_loki.add(
        event="llm_usage",
        agent_id=aid,
        payload={
            "in_total": 200_000,
            "out_total": 40_000,
            "cache_read": 0,
            "model": "claude-opus-4-8",
            "cost_usd": 2.0,
        },
        ts_offset_hours=2,
    )
    fake_loki.add(
        event="turn_end",
        agent_id=aid,
        payload={"duration_seconds": 4.0, "ok": True},
        ts_offset_hours=2,
    )
    db_conn.commit()
    with TestClient(app) as client:
        body = client.get(
            f"/api/agents/{aid}/inspect", params={"since_compact": "true", "hours": 1}
        ).json()
    assert body["since_compact"] is True
    assert body["window_hours"] is None
    # only the post-compact call: its stored snapshot, 2.0
    assert body["cost"]["llm_calls"] == 1
    assert body["cost"]["cost_usd"] == pytest.approx(2.0)  # pyright: ignore[reportUnknownMemberType]
    assert body["stats"]["turn_total"] == 1
    assert body["stats"]["turn_ok"] == 1


def test_inspect_since_compact_never_compacted_is_cumulative(
    db_conn: psycopg.Connection, fake_loki: _FakeLoki
) -> None:
    """Agent never compacted + since_compact=true → entire lifetime is in window."""
    aid = _insert_agent(db_conn)
    _ledger_row(db_conn, agent_id=aid, days_ago=9, tin=1_000_000, tout=1_000_000, cost=30.0)
    db_conn.commit()
    with TestClient(app) as client:
        body = client.get(f"/api/agents/{aid}/inspect", params={"since_compact": "true"}).json()
    assert body["since_compact"] is True
    assert body["cost"]["llm_calls"] == 1
    assert body["cost"]["cost_usd"] == pytest.approx(30.0)  # pyright: ignore[reportUnknownMemberType]


@pytest.mark.parametrize("bad", ["5", "-1", "169", "abc", "24.5"])
def test_inspect_invalid_hours_422(db_conn: psycopg.Connection, bad: str) -> None:
    """hours outside {0,1,6,24,72,168} → 422 (fail-fast, reusing StatsWindowHours)."""
    aid = _insert_agent(db_conn)
    db_conn.commit()
    with TestClient(app) as client:
        resp = client.get(f"/api/agents/{aid}/inspect", params={"hours": bad})
    assert resp.status_code == 422


# ── Heartbeat section ──────────────────────────────────────────────────────


def test_inspect_heartbeat_running_agent_dashes(db_conn: psycopg.Connection) -> None:
    """running agent does not receive check-ins → next_at/paused_until both None; never paused →
    last_pause None. interval_s echoes the configuration value."""
    aid = _insert_agent(db_conn, status="running")
    db_conn.commit()
    with TestClient(app) as client:
        hb = client.get(f"/api/agents/{aid}/inspect").json()["heartbeat"]
    assert hb["interval_s"] == int(settings.daemon.heartbeat_interval_seconds)
    assert hb["next_at"] is None
    assert hb["paused_until"] is None
    assert hb["last_pause"] is None


def test_inspect_heartbeat_idle_projects_next_at(db_conn: psycopg.Connection) -> None:
    """idle + no pause → next_at = effective_last_active + idle_threshold_s;
    paused_until None. Parked 120s ago, so next_at ≈ now + (idle_threshold - 120)s."""
    aid = _insert_agent(db_conn, status="idling", status_changed_s_ago=120)
    db_conn.commit()
    with TestClient(app) as client:
        hb = client.get(f"/api/agents/{aid}/inspect").json()["heartbeat"]
    assert hb["paused_until"] is None
    assert hb["next_at"] is not None
    expected = settings.daemon.heartbeat_idle_threshold_seconds - 120
    assert _seconds_from_now(hb["next_at"]) == pytest.approx(expected, abs=5)  # pyright: ignore[reportUnknownMemberType]


def test_inspect_heartbeat_paused_shows_window(db_conn: psycopg.Connection) -> None:
    """idle + future heartbeat_paused_until → paused_until pass-through, next_at None."""
    aid = _insert_agent(
        db_conn, status="idling", status_changed_s_ago=600, paused_until_s_ahead=1800
    )
    db_conn.commit()
    with TestClient(app) as client:
        hb = client.get(f"/api/agents/{aid}/inspect").json()["heartbeat"]
    assert hb["next_at"] is None
    assert hb["paused_until"] is not None
    assert _seconds_from_now(hb["paused_until"]) == pytest.approx(1800, abs=5)  # pyright: ignore[reportUnknownMemberType]


def test_inspect_heartbeat_expired_pause_is_not_paused(
    db_conn: psycopg.Connection, fake_loki: _FakeLoki
) -> None:
    """Expired pause (heartbeat_paused_until in the past) treated as no pause → next_at projected normally,
    paused_until None. Consistent with daemon's `<= now()` check."""
    aid = _insert_agent(
        db_conn, status="idling", status_changed_s_ago=60, paused_until_s_ahead=-120
    )
    db_conn.commit()
    with TestClient(app) as client:
        hb = client.get(f"/api/agents/{aid}/inspect").json()["heartbeat"]
    assert hb["paused_until"] is None
    assert hb["next_at"] is not None


def test_inspect_heartbeat_pending_inbound_marks_heartbeat_pending(
    db_conn: psycopg.Connection,
) -> None:
    """idle + no pause + has pending inbound (daemon just sent check-in but agent hasn't processed) →
    heartbeat_pending=True, next_at None. Mirrors daemon's `NOT EXISTS (pending inbound)`
    guard: since a wake is already queued, daemon will not enqueue another check-in, so no future
    time can be projected. last_active_at stopped 500s ago (long expired), if projected normally
    would yield a 'past' next_at — exactly the root cause of the stuck agent showing 'one hour ago'
    in the original bug."""
    aid = _insert_agent(db_conn, status="idling", status_changed_s_ago=500)
    _insert_pending_inbound(db_conn, agent_id=aid)
    db_conn.commit()
    with TestClient(app) as client:
        hb = client.get(f"/api/agents/{aid}/inspect").json()["heartbeat"]
    assert hb["paused_until"] is None
    assert hb["heartbeat_pending"] is True
    assert hb["next_at"] is None


def test_inspect_heartbeat_no_pending_projects_from_last_active(
    db_conn: psycopg.Connection,
    fake_loki: _FakeLoki,
) -> None:
    """No pending inbound → next_at is based on last_active_at, unaffected by historic
    heartbeat_nudged events: once a check-in is processed and consumed, inbound is no longer pending,
    and that turn pushed last_active_at past the check-in time, so last_active_at alone is the correct
    baseline (the old event-floor was therefore redundant). heartbeat_pending False."""
    aid = _insert_agent(db_conn, status="idling", status_changed_s_ago=120)
    # a long-processed old check-in event (600s ago) — no longer a floor, no effect on projection.
    fake_loki.add(
        event="heartbeat_nudged",
        agent_id=aid,
        payload={"idle_minutes": 5},
        ts_offset_hours=600 / 3600,
    )
    db_conn.commit()
    with TestClient(app) as client:
        hb = client.get(f"/api/agents/{aid}/inspect").json()["heartbeat"]
    assert hb["heartbeat_pending"] is False
    assert hb["paused_until"] is None
    assert hb["next_at"] is not None
    # next_at is based on last_active_at (120s ago).
    expected = settings.daemon.heartbeat_idle_threshold_seconds - 120
    assert _seconds_from_now(hb["next_at"]) == pytest.approx(expected, abs=5)  # pyright: ignore[reportUnknownMemberType]


def test_inspect_heartbeat_stuck_after_expired_pause_no_past_next_at(
    db_conn: psycopg.Connection,
) -> None:
    """Regression (original bug): agent paused 5 minutes two hours ago, then stuck and not processing
    check-ins. The panel should show heartbeat_pending (a check-in is already queued), not project
    next_at as 'one hour ago' (a past time). Reproduction condition: last_active_at stopped 2h ago,
    pause expired, one pending heartbeat inbound (daemon because of pending guard does not re-send)."""
    aid = _insert_agent(
        db_conn,
        status="idling",
        status_changed_s_ago=7200,  # last_active_at 2h ago
        paused_until_s_ahead=-6900,  # paused_until 1h55m ago (expired)
    )
    # daemon sent a check-in after pause expired, but agent stuck and never processed → inbound still pending.
    _insert_pending_inbound(db_conn, agent_id=aid)
    db_conn.commit()
    with TestClient(app) as client:
        hb = client.get(f"/api/agents/{aid}/inspect").json()["heartbeat"]
    # expired pause treated as no pause.
    assert hb["paused_until"] is None
    # key assertion: does not project a past next_at (original bug would show "one hour ago").
    assert hb["next_at"] is None
    assert hb["heartbeat_pending"] is True


def test_inspect_heartbeat_last_pause_newest_wins(
    db_conn: psycopg.Connection, fake_loki: _FakeLoki
) -> None:
    """last_pause takes the most recent heartbeat_paused event, duration_s from payload;
    another agent's pause does not leak."""
    aid = _insert_agent(db_conn, status="idling", status_changed_s_ago=60)
    other = _insert_agent(db_conn, status="idling", status_changed_s_ago=60)
    # 5h old pause + 1h new pause — new one wins
    fake_loki.add(
        event="heartbeat_paused",
        agent_id=aid,
        payload={"duration_s": 3600},
        ts_offset_hours=5,
    )
    fake_loki.add(
        event="heartbeat_paused",
        agent_id=aid,
        payload={"duration_s": 1800},
        ts_offset_hours=1,
    )
    # another agent's pause — must not appear in this agent's last_pause
    fake_loki.add(
        event="heartbeat_paused",
        agent_id=other,
        payload={"duration_s": 999},
        ts_offset_hours=0,
    )
    db_conn.commit()
    with TestClient(app) as client:
        hb = client.get(f"/api/agents/{aid}/inspect").json()["heartbeat"]
    assert hb["last_pause"] is not None
    assert hb["last_pause"]["duration_s"] == 1800
    # at ≈ now - 1h
    assert _seconds_from_now(hb["last_pause"]["at"]) == pytest.approx(-3600, abs=60)  # pyright: ignore[reportUnknownMemberType]


def test_inspect_heartbeat_last_pause_beyond_lookback_is_none(
    db_conn: psycopg.Connection, fake_loki: _FakeLoki
) -> None:
    """A pause older than the recent-history lookback is omitted."""
    aid = _insert_agent(db_conn, status="idling", status_changed_s_ago=60)
    fake_loki.add(
        event="heartbeat_paused",
        agent_id=aid,
        payload={"duration_s": 3600},
        ts_offset_hours=30,
    )
    db_conn.commit()
    with TestClient(app) as client:
        hb = client.get(f"/api/agents/{aid}/inspect").json()["heartbeat"]
    assert hb["last_pause"] is None


# ── Notice section ──────────────────────────────────────────────────────────


def test_inspect_notice_when_agent_has_open_require_response(db_conn: psycopg.Connection) -> None:
    """An agent with a require_response notice → notice field present with all keys."""
    aid = _insert_agent(db_conn)
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO agent_notices (agent_id, local_id, title, content, priority, require_response, blocking) "
            "VALUES (%s, COALESCE((SELECT MAX(local_id) FROM agent_notices WHERE agent_id = %s), -1) + 1, "
            "'Approve deploy?', 'Can we deploy to prod?', 'P0', true, true) "
            "RETURNING id, created_at",
            (aid, aid),
        )
        row = cur.fetchone()
    assert row is not None
    nid, _created_at = row
    db_conn.commit()
    with TestClient(app) as client:
        body = client.get(f"/api/agents/{aid}/inspect").json()
    notice = body["notice"]
    assert notice is not None
    assert notice["id"] == nid
    assert notice["title"] == "Approve deploy?"
    assert notice["content"] == "Can we deploy to prod?"
    assert notice["priority"] == "P0"
    assert notice["require_response"] is True
    assert notice["blocking"] is True


def test_inspect_notice_when_agent_has_open_fyi(db_conn: psycopg.Connection) -> None:
    """An agent with an FYI notice → notice field present with require_response=False."""
    aid = _insert_agent(db_conn)
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO agent_notices (agent_id, local_id, title, content, priority, require_response, blocking) "
            "VALUES (%s, COALESCE((SELECT MAX(local_id) FROM agent_notices WHERE agent_id = %s), -1) + 1, "
            "'Milestone reached', NULL, 'P2', false, false) "
            "RETURNING id",
            (aid, aid),
        )
        row = cur.fetchone()
    assert row is not None
    nid = row[0]
    db_conn.commit()
    with TestClient(app) as client:
        body = client.get(f"/api/agents/{aid}/inspect").json()
    notice = body["notice"]
    assert notice is not None
    assert notice["id"] == nid
    assert notice["require_response"] is False
    assert notice["blocking"] is False
    assert notice["content"] is None


def test_inspect_notice_when_agent_has_none(db_conn: psycopg.Connection) -> None:
    """An agent with no open notices → notice is None (not missing, not empty dict)."""
    aid = _insert_agent(db_conn)
    db_conn.commit()
    with TestClient(app) as client:
        body = client.get(f"/api/agents/{aid}/inspect").json()
    assert body["notice"] is None


def test_inspect_notice_resolved_not_returned(db_conn: psycopg.Connection) -> None:
    """A resolved notice is not returned; only open (resolved_at IS NULL) counts."""
    aid = _insert_agent(db_conn)
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO agent_notices (agent_id, local_id, title, priority, require_response, blocking, "
            "resolved_at, resolution, reply) VALUES (%s, COALESCE((SELECT MAX(local_id) FROM agent_notices "
            "WHERE agent_id = %s), -1) + 1, 'Old', 'P3', true, false, now(), 'answered', 'done')",
            (aid, aid),
        )
        # And a newer open one — the newer wins since we ORDER BY created_at DESC LIMIT 1
        cur.execute(
            "INSERT INTO agent_notices (agent_id, local_id, title, priority, require_response, blocking) "
            "VALUES (%s, COALESCE((SELECT MAX(local_id) FROM agent_notices WHERE agent_id = %s), -1) + 1, "
            "'Current', 'P1', false, false)",
            (aid, aid),
        )
    db_conn.commit()
    with TestClient(app) as client:
        body = client.get(f"/api/agents/{aid}/inspect").json()
    assert body["notice"] is not None
    assert body["notice"]["title"] == "Current"


def test_inspect_notice_other_agent_not_visible(db_conn: psycopg.Connection) -> None:
    """Another agent's notice does not leak into this agent's inspect."""
    aid = _insert_agent(db_conn)
    other = _insert_agent(db_conn)
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO agent_notices (agent_id, local_id, title, priority, require_response, blocking) "
            "VALUES (%s, COALESCE((SELECT MAX(local_id) FROM agent_notices WHERE agent_id = %s), -1) + 1, "
            "'Other notice', 'P0', true, true)",
            (other, other),
        )
    db_conn.commit()
    with TestClient(app) as client:
        body = client.get(f"/api/agents/{aid}/inspect").json()
    assert body["notice"] is None


def test_inspect_tps_empty_agent_zeros(db_conn: psycopg.Connection) -> None:
    """Agent with no LLM calls and no turn_end events → both TPS are 0."""
    aid = _insert_agent(db_conn)
    db_conn.commit()
    with TestClient(app) as client:
        body = client.get(f"/api/agents/{aid}/inspect").json()
    tps = body["tps"]
    assert tps["lm_stage_tps"] == 0.0
    assert tps["agent_lifecycle_tps"] == 0.0


def test_inspect_tps_lm_stage(db_conn: psycopg.Connection, fake_loki: _FakeLoki) -> None:
    """LM-stage TPS = output tokens / sum of turn_end durations."""
    aid = _insert_agent(db_conn)
    # 1M out tokens total
    fake_loki.add(
        event="llm_usage",
        agent_id=aid,
        payload={
            "in_total": 1_000_000,
            "out_total": 1_000_000,
            "cache_read": 0,
            "model": "claude-opus-4-8",
        },
    )
    # Two turns, 10s + 15s = 25s of LLM time
    for dur, ok in ((10.0, True), (15.0, True)):
        fake_loki.add(
            event="turn_end",
            agent_id=aid,
            payload={"duration_seconds": dur, "ok": ok},
        )
    db_conn.commit()
    with TestClient(app) as client:
        body = client.get(f"/api/agents/{aid}/inspect").json()
    tps = body["tps"]
    # 1,000,000 output tokens / 25s = 40,000 tps
    assert tps["lm_stage_tps"] == 40000.0


def test_inspect_tps_lifecycle_from_events(
    db_conn: psycopg.Connection, fake_loki: _FakeLoki
) -> None:
    """Agent-lifecycle TPS uses lifecycle events for alive time."""
    aid = _insert_agent(db_conn)
    # Simulate lifecycle: spawned → (runs 10s) → terminated → resurrected → (running now)
    fake_loki.add(event="agent_spawned", agent_id=aid, ts_offset_hours=1)
    # 1 hour ago: spawned
    fake_loki.add(
        event="agent_terminated",
        agent_id=aid,
        ts_offset_hours=0.5,
    )
    # 0.5 hours ago: terminated (first life = 0.5h)
    fake_loki.add(
        event="agent_resurrected",
        agent_id=aid,
        ts_offset_hours=0.25,
    )
    # 0.25 hours ago: resurrected → currently alive
    # Tokens: 500 out
    fake_loki.add(
        event="llm_usage",
        agent_id=aid,
        payload={
            "in_total": 1000,
            "out_total": 500,
            "cache_read": 0,
            "model": "claude-opus-4-8",
        },
    )
    db_conn.commit()
    with TestClient(app) as client:
        body = client.get(f"/api/agents/{aid}/inspect").json()
    tps = body["tps"]
    # First life: 1h - 0.5h = 0.5h = 1800s
    # Second life: 0.25h ago → now ≈ 0.25h = 900s
    # Total alive ≈ 2700s
    # TPS ≈ 500 / 2700 ≈ 0.19
    assert tps["agent_lifecycle_tps"] > 0
    assert tps["agent_lifecycle_tps"] < 2  # rough sanity check


def test_inspect_tps_hours_windows_tps(db_conn: psycopg.Connection, fake_loki: _FakeLoki) -> None:
    """?hours= window only includes llm_usage and turn_end within the window.
    LM-stage TPS narrows both numerator and denominator; lifecycle TPS narrows
    only numerator."""
    aid = _insert_agent(db_conn)
    # Recent turn + usage (within 1h)
    fake_loki.add(
        event="llm_usage",
        agent_id=aid,
        payload={"in_total": 100, "out_total": 50, "model": "claude-opus-4-8"},
        ts_offset_hours=0,
    )
    fake_loki.add(
        event="turn_end",
        agent_id=aid,
        payload={"duration_seconds": 2.0, "ok": True},
        ts_offset_hours=0,
    )
    # Old turn + usage (outside 1h window)
    fake_loki.add(
        event="llm_usage",
        agent_id=aid,
        payload={
            "in_total": 1_000_000,
            "out_total": 1_000_000,
            "model": "claude-opus-4-8",
        },
        ts_offset_hours=5,
    )
    fake_loki.add(
        event="turn_end",
        agent_id=aid,
        payload={"duration_seconds": 100.0, "ok": True},
        ts_offset_hours=5,
    )
    db_conn.commit()
    with TestClient(app) as client:
        body = client.get(f"/api/agents/{aid}/inspect?hours=1").json()
    tps = body["tps"]
    # LM-stage: 50 output tokens / 2s = 25 tps (old events excluded)
    assert tps["lm_stage_tps"] == 25.0
    # Lifecycle: 50 output tokens / total_alive_time (roughly > 0)
    assert tps["agent_lifecycle_tps"] > 0


def test_inspect_tps_field_present_in_response(
    db_conn: psycopg.Connection, fake_loki: _FakeLoki
) -> None:
    """Every inspect response includes the tps object with both fields."""
    aid = _insert_agent(db_conn)
    db_conn.commit()
    with TestClient(app) as client:
        body = client.get(f"/api/agents/{aid}/inspect").json()
    assert "tps" in body
    assert "lm_stage_tps" in body["tps"]
    assert "agent_lifecycle_tps" in body["tps"]
    assert isinstance(body["tps"]["lm_stage_tps"], (int, float))
    assert isinstance(body["tps"]["agent_lifecycle_tps"], (int, float))


# ── Activity (active-rate) section ────────────────────────────────────────────


def _node_exit(
    fake_loki: _FakeLoki,
    *,
    agent_id: int,
    node: str,
    duration_seconds: float,
    ts_offset_hours: float = 0,
) -> None:
    """A `node_exit` event row — the per-node duration the active-rate
    numerator sums (non-`claim` nodes only)."""
    fake_loki.add(
        event="node_exit",
        agent_id=agent_id,
        payload={"node": node, "duration_seconds": duration_seconds, "outcome": "ok"},
        ts_offset_hours=ts_offset_hours,
    )


def test_inspect_activity_field_present(db_conn: psycopg.Connection) -> None:
    """Every inspect response includes the activity object with all five fields."""
    aid = _insert_agent(db_conn)
    db_conn.commit()
    with TestClient(app) as client:
        body = client.get(f"/api/agents/{aid}/inspect").json()
    assert "activity" in body
    act = body["activity"]
    assert set(act) == {
        "active_seconds",
        "alive_seconds",
        "active_rate",
        "llm_seconds",
        "exec_seconds",
    }
    assert all(isinstance(act[k], (int, float)) for k in act)


def test_inspect_activity_empty_agent_zeros(db_conn: psycopg.Connection) -> None:
    """An agent with no node_exit events → active_seconds 0 and active_rate 0
    (no divide-by-zero even when alive is ~0)."""
    aid = _insert_agent(db_conn)
    db_conn.commit()
    with TestClient(app) as client:
        act = client.get(f"/api/agents/{aid}/inspect").json()["activity"]
    assert act["active_seconds"] == 0
    assert act["active_rate"] == 0


def test_inspect_activity_sums_non_claim_nodes(
    db_conn: psycopg.Connection, fake_loki: _FakeLoki
) -> None:
    """active_seconds = Σ node_exit.duration over llm + exec + hooks; alive from
    the lifecycle open tail; active_rate = active/alive."""
    aid = _insert_agent(db_conn)
    # Alive: spawned 1h ago, still running → alive ≈ 3600s.
    fake_loki.add(event="agent_spawned", agent_id=aid, ts_offset_hours=1)
    # Active: 10s llm + 20s exec + 5s before_llm hook = 35s of real processing.
    _node_exit(fake_loki, agent_id=aid, node="llm", duration_seconds=10.0)
    _node_exit(fake_loki, agent_id=aid, node="exec", duration_seconds=20.0)
    _node_exit(fake_loki, agent_id=aid, node="before_llm", duration_seconds=5.0)
    db_conn.commit()
    with TestClient(app) as client:
        act = client.get(f"/api/agents/{aid}/inspect").json()["activity"]
    assert act["active_seconds"] == pytest.approx(35.0)  # pyright: ignore[reportUnknownMemberType]
    assert act["alive_seconds"] == pytest.approx(3600, abs=30)  # pyright: ignore[reportUnknownMemberType]
    # 35 / 3600 ≈ 0.0097
    assert act["active_rate"] == pytest.approx(35.0 / act["alive_seconds"], abs=1e-3)  # pyright: ignore[reportUnknownMemberType]


def test_inspect_activity_excludes_claim_node(
    db_conn: psycopg.Connection, fake_loki: _FakeLoki
) -> None:
    """The `claim` node's wall-clock IS the idle-wait, so it is NOT active —
    a claim node_exit does not raise active_seconds."""
    aid = _insert_agent(db_conn)
    fake_loki.add(event="agent_spawned", agent_id=aid, ts_offset_hours=1)
    # A long claim (idle-wait) + a short llm turn.
    _node_exit(fake_loki, agent_id=aid, node="claim", duration_seconds=3000.0)
    _node_exit(fake_loki, agent_id=aid, node="llm", duration_seconds=12.0)
    db_conn.commit()
    with TestClient(app) as client:
        act = client.get(f"/api/agents/{aid}/inspect").json()["activity"]
    # only the llm node counts — the 3000s claim is excluded
    assert act["active_seconds"] == pytest.approx(12.0)  # pyright: ignore[reportUnknownMemberType]


def test_inspect_activity_counts_missing_node_in_any_category(
    db_conn: psycopg.Connection, fake_loki: _FakeLoki
) -> None:
    """Missing ``node`` still means non-claim, even outside telemetry/log."""
    aid = _insert_agent(db_conn)
    fake_loki.add(
        event="node_exit",
        agent_id=aid,
        category="audit",
        payload={"duration_seconds": 3.0},
    )
    db_conn.commit()

    with TestClient(app) as client:
        activity = client.get(f"/api/agents/{aid}/inspect").json()["activity"]

    assert activity["active_seconds"] == 3.0
    assert activity["exec_seconds"] == 0.0


def test_inspect_activity_rate_capped_at_one(
    db_conn: psycopg.Connection, fake_loki: _FakeLoki
) -> None:
    """A node whose span began before the window's leading edge counts in full,
    so raw active can exceed windowed alive — active_rate is capped at 1.0 while
    the raw seconds are preserved."""
    aid = _insert_agent(db_conn)
    # Alive only ~10s, but a node_exit reports 100s of processing.
    fake_loki.add(event="agent_spawned", agent_id=aid, ts_offset_hours=10.0 / 3600)
    _node_exit(fake_loki, agent_id=aid, node="exec", duration_seconds=100.0)
    db_conn.commit()
    with TestClient(app) as client:
        act = client.get(f"/api/agents/{aid}/inspect").json()["activity"]
    assert act["active_seconds"] == pytest.approx(100.0)  # pyright: ignore[reportUnknownMemberType]
    assert act["active_rate"] == 1.0


def test_inspect_activity_windows_both_active_and_alive(
    db_conn: psycopg.Connection, fake_loki: _FakeLoki
) -> None:
    """`?hours=1` clips BOTH the numerator (node_exit ts) and the denominator
    (alive-time lower bound), unlike lifecycle-tps which windows only tokens."""
    aid = _insert_agent(db_conn)
    # Alive: spawned 5h ago, still running.
    fake_loki.add(event="agent_spawned", agent_id=aid, ts_offset_hours=5)
    # 5h-ago node_exit is outside the 1h window; 0.5h-ago one is inside.
    _node_exit(fake_loki, agent_id=aid, node="llm", duration_seconds=10.0, ts_offset_hours=5)
    _node_exit(fake_loki, agent_id=aid, node="exec", duration_seconds=20.0, ts_offset_hours=0.5)
    db_conn.commit()
    with TestClient(app) as client:
        windowed = client.get(f"/api/agents/{aid}/inspect", params={"hours": 1}).json()["activity"]
        cumulative = client.get(f"/api/agents/{aid}/inspect").json()["activity"]
    # windowed: only the in-window 20s; alive clipped to the last 1h ≈ 3600s.
    assert windowed["active_seconds"] == pytest.approx(20.0)  # pyright: ignore[reportUnknownMemberType]
    assert windowed["alive_seconds"] == pytest.approx(3600, abs=30)  # pyright: ignore[reportUnknownMemberType]
    # cumulative: both node_exits = 30s; alive ≈ 5h = 18000s.
    assert cumulative["active_seconds"] == pytest.approx(30.0)  # pyright: ignore[reportUnknownMemberType]
    assert cumulative["alive_seconds"] == pytest.approx(18000, abs=60)  # pyright: ignore[reportUnknownMemberType]


def test_inspect_activity_scoped_to_agent(
    db_conn: psycopg.Connection, fake_loki: _FakeLoki
) -> None:
    """Another agent's node_exit events do not inflate this agent's active_seconds."""
    aid = _insert_agent(db_conn)
    other = _insert_agent(db_conn)
    fake_loki.add(event="agent_spawned", agent_id=aid, ts_offset_hours=1)
    _node_exit(fake_loki, agent_id=other, node="llm", duration_seconds=999.0)
    db_conn.commit()
    with TestClient(app) as client:
        act = client.get(f"/api/agents/{aid}/inspect").json()["activity"]
    assert act["active_seconds"] == 0


def test_inspect_activity_terminate_only_falls_back_to_spawned_at(
    db_conn: psycopg.Connection,
    fake_loki: _FakeLoki,
) -> None:
    """A partial lifecycle stream with ONLY `agent_terminated` (no start — the
    agent predates `agent_spawned` emission) still recovers a known lifetime from
    `spawned_at`, so alive is non-zero and active_rate is computable — not
    spuriously 0. The fallback triggers on 'no start seen', not 'events empty'."""
    aid = _insert_agent(db_conn)
    with db_conn.cursor() as cur:
        cur.execute(
            "UPDATE agents_meta SET spawned_at = now() - interval '2 hours' WHERE id = %s",
            (aid,),
        )
    fake_loki.add(event="agent_terminated", agent_id=aid, ts_offset_hours=0.5)
    _node_exit(fake_loki, agent_id=aid, node="llm", duration_seconds=60.0)
    db_conn.commit()
    with TestClient(app) as client:
        act = client.get(f"/api/agents/{aid}/inspect").json()["activity"]
    # spawned_at → now ≈ 2h = 7200s (no start event to anchor an interval)
    assert act["alive_seconds"] == pytest.approx(7200, abs=30)  # pyright: ignore[reportUnknownMemberType]
    assert act["active_rate"] > 0


def test_inspect_activity_started_but_dead_before_window_is_zero(
    db_conn: psycopg.Connection,
    fake_loki: _FakeLoki,
) -> None:
    """An agent WITH a real start whose only life ended before the window reports
    0 alive in-window (it was not alive then) — it must NOT fall back to
    spawned_at. Guards the windowed denominator against the `saw_start` fallback."""
    aid = _insert_agent(db_conn)
    # spawned 5h ago, terminated 3h ago; a 1h window sits entirely after death.
    fake_loki.add(event="agent_spawned", agent_id=aid, ts_offset_hours=5)
    fake_loki.add(event="agent_terminated", agent_id=aid, ts_offset_hours=3)
    _node_exit(fake_loki, agent_id=aid, node="llm", duration_seconds=10.0, ts_offset_hours=4)
    db_conn.commit()
    with TestClient(app) as client:
        act = client.get(f"/api/agents/{aid}/inspect", params={"hours": 1}).json()["activity"]
    assert act["alive_seconds"] == 0
    assert act["active_rate"] == 0


def test_inspect_activity_resurrect_without_terminate_keeps_prior_interval(
    db_conn: psycopg.Connection,
    fake_loki: _FakeLoki,
) -> None:
    """A resurrect with no intervening terminate (the crash/reaper shape) must
    NOT discard the pre-crash life: alive = spawn→resurrect + resurrect→now. The
    old behavior silently overwrote the open start, so a long-idle agent whose
    crashed process was auto-resurrected reported alive ≈ only the latest life,
    letting active_seconds exceed alive_seconds and pinning active_rate at 1.0 —
    which rendered the inspector's Idle cell as 0s."""
    aid = _insert_agent(db_conn)
    # spawned 5h ago, crashed (no agent_terminated), resurrected 2h ago, alive now.
    fake_loki.add(event="agent_spawned", agent_id=aid, ts_offset_hours=5)
    fake_loki.add(event="agent_resurrected", agent_id=aid, ts_offset_hours=2)
    # 1h of real work across the two lives — well under the ~5h alive.
    _node_exit(fake_loki, agent_id=aid, node="llm", duration_seconds=1800.0, ts_offset_hours=4)
    _node_exit(fake_loki, agent_id=aid, node="llm", duration_seconds=1800.0, ts_offset_hours=1)
    db_conn.commit()
    with TestClient(app) as client:
        act = client.get(f"/api/agents/{aid}/inspect").json()["activity"]
    # spawn→resurrect (3h) + resurrect→now (2h) ≈ 5h = 18000s, not just 2h.
    assert act["alive_seconds"] == pytest.approx(18000, abs=60)  # pyright: ignore[reportUnknownMemberType]
    # 3600s active over 18000s alive — comfortably under the 1.0 cap.
    assert act["active_seconds"] == pytest.approx(3600.0)  # pyright: ignore[reportUnknownMemberType]
    assert act["active_rate"] == pytest.approx(0.2, abs=1e-3)  # pyright: ignore[reportUnknownMemberType]


def test_inspect_activity_multiple_resurrects_sum_all_intervals(
    db_conn: psycopg.Connection,
    fake_loki: _FakeLoki,
) -> None:
    """spawn → resurrect → resurrect (all without terminates) sums every
    interval: each consecutive start closes the previous open life at its own
    timestamp instead of overwriting it."""
    aid = _insert_agent(db_conn)
    # spawned 10h ago, resurrected 6h ago, resurrected 2h ago, alive now.
    fake_loki.add(event="agent_spawned", agent_id=aid, ts_offset_hours=10)
    fake_loki.add(event="agent_resurrected", agent_id=aid, ts_offset_hours=6)
    fake_loki.add(event="agent_resurrected", agent_id=aid, ts_offset_hours=2)
    db_conn.commit()
    with TestClient(app) as client:
        act = client.get(f"/api/agents/{aid}/inspect").json()["activity"]
    # 4h + 4h + 2h = 10h = 36000s.
    assert act["alive_seconds"] == pytest.approx(36000, abs=60)  # pyright: ignore[reportUnknownMemberType]


def test_inspect_activity_resurrect_without_terminate_windows_clip(
    db_conn: psycopg.Connection,
    fake_loki: _FakeLoki,
) -> None:
    """A windowed request clips the closed pre-crash interval the same way it
    clips any other interval: only the parts inside [window_start, now] count —
    the pre-crash interval's in-window tail PLUS the current life."""
    aid = _insert_agent(db_conn)
    # spawned 5h ago, crashed, resurrected 0.5h ago, alive now.
    fake_loki.add(event="agent_spawned", agent_id=aid, ts_offset_hours=5)
    fake_loki.add(event="agent_resurrected", agent_id=aid, ts_offset_hours=0.5)
    db_conn.commit()
    with TestClient(app) as client:
        act = client.get(f"/api/agents/{aid}/inspect", params={"hours": 1}).json()["activity"]
    # In the 1h window: [now-1h, resurrect] (0.5h) + [resurrect, now] (0.5h) = 1h.
    assert act["alive_seconds"] == pytest.approx(3600, abs=30)  # pyright: ignore[reportUnknownMemberType]


# ── Response-cache discipline (the panel refetches in bursts) ─────────────────


def test_inspect_response_cache_absorbs_repeat_burst(
    db_conn: psycopg.Connection, fake_loki: _FakeLoki
) -> None:
    """A repeat call within the TTL serves the cached aggregates — the panel's
    refetch bursts (open + notice SSE invalidation + 60s interval) must not
    re-run the Loki fan-out per request. The notice is NOT cached: it must
    reflect a change made after the first call immediately."""
    aid = _insert_agent(db_conn)
    fake_loki.add(event="turn_end", agent_id=aid, payload={"duration_seconds": 2.0, "ok": True})
    db_conn.commit()
    with TestClient(app) as client:
        first = client.get(f"/api/agents/{aid}/inspect").json()
        assert first["stats"]["turn_total"] == 1

        # More events land AFTER the first call; a cached serve must not see them.
        fake_loki.add(event="turn_end", agent_id=aid, payload={"duration_seconds": 4.0, "ok": True})
        fake_loki.add(event="turn_end", agent_id=aid, payload={"duration_seconds": 6.0, "ok": True})
        second = client.get(f"/api/agents/{aid}/inspect").json()
        assert second["stats"]["turn_total"] == 1  # cached, pre-burst view

        # ... but a newly opened notice appears on the very next call.
        db_conn.cursor().execute(
            "INSERT INTO agent_notices (agent_id, local_id, title, content, priority, require_response, blocking) "
            "VALUES (%s, COALESCE((SELECT MAX(local_id) FROM agent_notices WHERE agent_id = %s), -1) + 1, "
            "'q', 'q', 'P2', true, false)",
            (aid, aid),
        )
        db_conn.commit()
        third = client.get(f"/api/agents/{aid}/inspect").json()
        assert third["notice"] is not None
        assert third["notice"]["title"] == "q"


def test_inspect_cache_keeps_only_aggregates_and_refreshes_live_db_fields(
    db_conn: psycopg.Connection,
    fake_loki: _FakeLoki,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The 75s TTL may retain Loki/ledger aggregates, never live DB state.

    This models both the 60s poll (monotonic +60, still inside the aggregate
    TTL) and a manual refresh (a second HTTP request): machine, config,
    heartbeat inputs, and liveness must come from a fresh agents_meta read,
    while the historical turn aggregate remains cached.
    """
    clock = [1_000.0]
    monkeypatch.setattr(agent_inspect.time_mod, "monotonic", lambda: clock[0])
    aid = _insert_agent(
        db_conn,
        status="idling",
        config_overlay={"llm_model": "model-a"},
        status_changed_s_ago=120,
    )
    fake_loki.add(event="turn_end", agent_id=aid, payload={"duration_seconds": 2.0, "ok": True})
    with db_conn.cursor() as cur:
        cur.execute(
            "UPDATE agents_meta SET machine = 'runner-a', liveness_state = 'online', "
            "last_probe_at = now() WHERE id = %s",
            (aid,),
        )
    db_conn.commit()

    with TestClient(app) as client:
        first = client.get(f"/api/agents/{aid}/inspect").json()
        assert first["machine"] == "runner-a"
        assert first["config_overlay"] == {"llm_model": "model-a"}
        assert first["liveness_state"] == "online"
        assert first["last_probe_at"] is not None
        assert first["heartbeat"]["next_at"] is not None
        assert first["stats"]["turn_total"] == 1

        fake_loki.add(
            event="turn_end",
            agent_id=aid,
            payload={"duration_seconds": 4.0, "ok": True},
        )
        with db_conn.cursor() as cur:
            cur.execute(
                "UPDATE agents_meta SET machine = 'runner-b', status = 'running', "
                'config_overlay = \'{"llm_model": "model-b"}\'::jsonb, '
                "liveness_state = 'offline', last_probe_at = NULL WHERE id = %s",
                (aid,),
            )
        db_conn.commit()

        # Immediate/manual refresh: no monotonic time passes, but every live
        # field must already be B while the Loki aggregate stays A.
        manual = client.get(f"/api/agents/{aid}/inspect").json()
        assert manual["machine"] == "runner-b"
        assert manual["config_overlay"] == {"llm_model": "model-b"}
        assert manual["liveness_state"] == "offline"
        assert manual["last_probe_at"] is None
        assert manual["heartbeat"]["next_at"] is None
        assert manual["heartbeat"]["paused_until"] is None
        assert manual["stats"]["turn_total"] == 1

        with db_conn.cursor() as cur:
            cur.execute(
                "UPDATE agents_meta SET machine = 'runner-c', status = 'idling', "
                'config_overlay = \'{"llm_model": "model-c"}\'::jsonb, '
                "heartbeat_paused_until = now() + interval '10 minutes', "
                "liveness_state = 'unknown', last_probe_at = now() WHERE id = %s",
                (aid,),
            )
        db_conn.commit()

        # Background poll at t+60 is still an aggregate-cache hit (TTL=75),
        # yet the live projection must advance again to C.
        clock[0] += 60
        refreshed = client.get(f"/api/agents/{aid}/inspect").json()

    assert refreshed["machine"] == "runner-c"
    assert refreshed["config_overlay"] == {"llm_model": "model-c"}
    assert refreshed["liveness_state"] == "unknown"
    assert refreshed["last_probe_at"] is not None
    assert refreshed["heartbeat"]["next_at"] is None
    assert refreshed["heartbeat"]["paused_until"] is not None
    # The historical aggregate is the only cached part; the new Loki row is
    # intentionally invisible until the 75s TTL expires.
    assert refreshed["stats"]["turn_total"] == 1


def test_inspect_releases_live_db_borrow_before_cached_loki_fanout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A queued/slow aggregate loader must never pin the live-state DB borrow."""
    now = datetime.now(UTC)

    class TrackingCursor:
        def __init__(self) -> None:
            self.rows: list[tuple[Any, ...]] = [
                ({}, "runner", "running", now, now, now, None, "online", now),
                (False,),
            ]

        def __enter__(self) -> TrackingCursor:
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def execute(self, query: str, params: tuple[int] | None = None) -> None:
            return None

        def fetchone(self) -> tuple[Any, ...]:
            return self.rows.pop(0)

    class TrackingConnection:
        def __enter__(self) -> TrackingConnection:
            pool.active = True
            return self

        def __exit__(self, *args: object) -> None:
            pool.active = False

        def cursor(self) -> TrackingCursor:
            return TrackingCursor()

    class TrackingPool:
        active = False

        def connection(self) -> TrackingConnection:
            return TrackingConnection()

    class StopAfterOrderingProofError(RuntimeError):
        pass

    pool = TrackingPool()

    async def cached_loader(*args: Any, **kwargs: Any) -> Any:
        assert pool.active is False
        raise StopAfterOrderingProofError

    monkeypatch.setattr(agent_inspect, "_inspect_rows_cached_async", cached_loader)

    class RequestState:
        db_pool = pool

    class RequestApp:
        state = RequestState()

    class FakeRequest:
        app = RequestApp()

    with pytest.raises(StopAfterOrderingProofError):
        asyncio.run(agent_inspect.get_agent_inspect(7, FakeRequest()))  # type: ignore[arg-type]


def test_inspect_response_cache_keyed_by_window(
    db_conn: psycopg.Connection, fake_loki: _FakeLoki
) -> None:
    """The cache key includes the window: hours=1 and whole-life are separate
    entries, so a burst on one window never serves the other's data."""
    aid = _insert_agent(db_conn)
    fake_loki.add(event="turn_end", agent_id=aid, payload={"duration_seconds": 1.0, "ok": True})
    db_conn.commit()
    with TestClient(app) as client:
        windowed = client.get(f"/api/agents/{aid}/inspect", params={"hours": 1}).json()
        assert windowed["stats"]["turn_total"] == 1

        # An event older than the 1h window lands after the windowed call.
        fake_loki.add(
            event="turn_end",
            agent_id=aid,
            payload={"duration_seconds": 9.0, "ok": True},
            ts_offset_hours=3,
        )
        # Whole-life is a different cache entry: it must see the old event.
        whole = client.get(f"/api/agents/{aid}/inspect").json()
        assert whole["stats"]["turn_total"] == 2
        # The windowed entry is still cached from before: no new event seen.
        windowed_again = client.get(f"/api/agents/{aid}/inspect", params={"hours": 1}).json()
        assert windowed_again["stats"]["turn_total"] == 1


def test_inspect_response_cache_expires(
    db_conn: psycopg.Connection,
    fake_loki: _FakeLoki,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """After the TTL the next call re-runs the fan-out and sees new events."""
    # Zero TTL → every call is a miss.
    monkeypatch.setattr(agent_inspect, "_INSPECT_CACHE_TTL_S", 0.0)
    aid = _insert_agent(db_conn)
    fake_loki.add(event="turn_end", agent_id=aid, payload={"duration_seconds": 2.0, "ok": True})
    db_conn.commit()
    with TestClient(app) as client:
        first = client.get(f"/api/agents/{aid}/inspect").json()
        assert first["stats"]["turn_total"] == 1

        fake_loki.add(event="turn_end", agent_id=aid, payload={"duration_seconds": 4.0, "ok": True})
        second = client.get(f"/api/agents/{aid}/inspect").json()
        assert second["stats"]["turn_total"] == 2


def test_inspect_singleflight_coalesces_concurrent_identical_fanout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """N simultaneous misses for one agent/window execute one expensive fan-out."""
    agent_inspect.cache_clear()
    started = threading.Event()
    release = threading.Event()
    calls = 0
    calls_lock = threading.Lock()
    expected = object()
    fake_pool: Any = object()
    followers_joined = _track_inspect_cache_followers(monkeypatch, expected=7)

    def fake_fanout(*args: Any, **kwargs: Any) -> object:
        nonlocal calls
        with calls_lock:
            calls += 1
        started.set()
        assert release.wait(timeout=2)
        return expected

    monkeypatch.setattr(agent_inspect, "_inspect_blocking", fake_fanout)
    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = [
            executor.submit(
                agent_inspect._inspect_rows_cached,
                fake_pool,
                41,
                None,
                since_compact=False,
            )
            for _ in range(8)
        ]
        assert started.wait(timeout=1)
        assert followers_joined.wait(timeout=1)
        assert calls == 1
        release.set()
        assert [future.result(timeout=1) for future in futures] == [expected] * 8


def test_inspect_fanout_preserves_one_global_loki_slot_for_other_queries() -> None:
    """One inspector stays parallel without monopolizing Loki's capacity.

    Three workers execute five independent sections in at most two waves for
    equal-duration queries, while the fourth Loki slot remains available to a
    queued stats/ops request.
    """
    assert agent_inspect._INSPECT_WORKERS == loki_query_budget.LOKI_QUERY_CONCURRENCY - 1


def _track_inspect_cache_followers(
    monkeypatch: pytest.MonkeyPatch, *, expected: int
) -> threading.Event:
    """Signal once the requested number of callers joined the leader Future."""
    cache: Any = agent_inspect._inspect_query_cache
    original = cache._lookup_or_claim
    joined = threading.Event()
    count = 0
    lock = threading.Lock()

    def tracked(key: Any, current: float) -> Any:
        nonlocal count
        claim = original(key, current)
        if getattr(claim, "leader", None) is False:
            with lock:
                count += 1
                if count == expected:
                    joined.set()
        return claim

    monkeypatch.setattr(cache, "_lookup_or_claim", tracked)
    return joined


def test_inspect_singleflight_failure_releases_key_for_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed leader wakes waiters and never leaves the key permanently bricked."""
    agent_inspect.cache_clear()
    started = threading.Event()
    release = threading.Event()
    calls = 0
    expected = object()
    fake_pool: Any = object()
    follower_joined = _track_inspect_cache_followers(monkeypatch, expected=1)

    def fake_fanout(*args: Any, **kwargs: Any) -> object:
        nonlocal calls
        calls += 1
        if calls == 1:
            started.set()
            assert release.wait(timeout=2)
            raise RuntimeError("loki unavailable")
        return expected

    monkeypatch.setattr(agent_inspect, "_inspect_blocking", fake_fanout)
    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(
            agent_inspect._inspect_rows_cached, fake_pool, 42, None, since_compact=False
        )
        second = executor.submit(
            agent_inspect._inspect_rows_cached, fake_pool, 42, None, since_compact=False
        )
        assert started.wait(timeout=1)
        assert follower_joined.wait(timeout=1)
        release.set()
        with pytest.raises(RuntimeError, match="loki unavailable"):
            first.result(timeout=1)
        with pytest.raises(RuntimeError, match="loki unavailable"):
            second.result(timeout=1)

    assert agent_inspect._inspect_rows_cached(fake_pool, 42, None, since_compact=False) is expected
    assert calls == 2


def test_inspect_cache_spans_one_sixty_second_poll_tick(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The retention-window fan-out is not repeated on every 60s panel tick."""
    agent_inspect.cache_clear()
    clock = [1_000.0]
    calls = 0
    fake_pool: Any = object()

    def fake_fanout(*args: Any, **kwargs: Any) -> object:
        nonlocal calls
        calls += 1
        return object()

    monkeypatch.setattr(agent_inspect.time_mod, "monotonic", lambda: clock[0])
    monkeypatch.setattr(agent_inspect, "_inspect_blocking", fake_fanout)
    first = agent_inspect._inspect_rows_cached(fake_pool, 43, None, since_compact=False)
    clock[0] += 60
    assert agent_inspect._inspect_rows_cached(fake_pool, 43, None, since_compact=False) is first
    assert calls == 1
    clock[0] += 20
    assert agent_inspect._inspect_rows_cached(fake_pool, 43, None, since_compact=False) is not first
    assert calls == 2


def test_inspect_singleflight_cancellation_releases_key_for_retry() -> None:
    """A cancelled leader never leaves its key in the in-flight map."""
    cache = InspectQueryCache[str, object](max_entries=2, max_inflight=1)
    calls = 0
    expected = object()

    def load() -> object:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise asyncio.CancelledError
        return expected

    with pytest.raises(asyncio.CancelledError):
        cache.get_or_load("same", load, ttl_s=10, now=lambda: 0)
    assert cache.get_or_load("same", load, ttl_s=10, now=lambda: 0) is expected
    assert calls == 2


@pytest.mark.asyncio
async def test_inspect_async_followers_timeout_without_retaining_executor_workers() -> None:
    """Timed-out followers wait on the shared Future without blocking threads."""
    cache = InspectQueryCache[str, object](max_entries=2, max_inflight=1)
    started = threading.Event()
    release = threading.Event()
    expected = object()
    calls = 0

    def blocking_load() -> object:
        nonlocal calls
        calls += 1
        started.set()
        assert release.wait(timeout=2)
        return expected

    leader = asyncio.create_task(
        cache.get_or_load_async("same", blocking_load, ttl_s=10, now=lambda: 0)
    )
    for _ in range(100):
        if started.is_set():
            break
        await asyncio.sleep(0.001)
    assert started.is_set()

    followers = [
        asyncio.create_task(
            asyncio.wait_for(
                cache.get_or_load_async(
                    "same",
                    lambda: pytest.fail("a follower must not load"),
                    ttl_s=10,
                    now=lambda: 0,
                ),
                timeout=0.01,
            )
        )
        for _ in range(64)
    ]
    results = await asyncio.gather(*followers, return_exceptions=True)
    assert all(isinstance(result, TimeoutError) for result in results)

    # A to_thread probe still starts immediately. An implementation that puts
    # every follower's Future.result() in the shared executor exhausts all of
    # its workers here until the leader is released.
    assert await asyncio.wait_for(asyncio.to_thread(lambda: "free"), timeout=1) == "free"
    assert calls == 1

    release.set()
    assert await asyncio.wait_for(leader, timeout=1) is expected
    assert (
        await cache.get_or_load_async(
            "same", lambda: pytest.fail("late result must be cached"), ttl_s=10, now=lambda: 0
        )
        is expected
    )


def test_inspect_cache_bounds_values_and_distinct_inflight_keys() -> None:
    """Both retained snapshots and active distinct-key loaders stay bounded."""
    cache = InspectQueryCache[str, str](max_entries=2, max_inflight=1)
    loads: dict[str, int] = {}

    def load(key: str) -> str:
        loads[key] = loads.get(key, 0) + 1
        return f"{key}-{loads[key]}"

    assert cache.get_or_load("a", lambda: load("a"), ttl_s=10, now=lambda: 0) == "a-1"
    assert cache.get_or_load("b", lambda: load("b"), ttl_s=10, now=lambda: 0) == "b-1"
    assert cache.get_or_load("c", lambda: load("c"), ttl_s=10, now=lambda: 0) == "c-1"
    # Insertion-order tie breaking evicts the oldest equal-expiry value.
    assert cache.get_or_load("a", lambda: load("a"), ttl_s=10, now=lambda: 0) == "a-2"

    started = threading.Event()
    release = threading.Event()

    def blocking_load() -> str:
        started.set()
        assert release.wait(timeout=2)
        return "held"

    with ThreadPoolExecutor(max_workers=1) as executor:
        holder = executor.submit(
            cache.get_or_load, "holder", blocking_load, ttl_s=10, now=lambda: 0
        )
        assert started.wait(timeout=1)
        with pytest.raises(InspectCacheFullError):
            cache.get_or_load("overflow", lambda: "no", ttl_s=10, now=lambda: 0)
        release.set()
        assert holder.result(timeout=1) == "held"
