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

import json
import re
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg
import pytest
from fastapi.testclient import TestClient

from gateway import loki_events
from gateway.app import app
from gateway.loki_events import _weighted_quantile
from shared.config import settings


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

    def add(
        self,
        *,
        event: str,
        agent_id: int,
        payload: dict[str, Any] | None = None,
        ts_offset_hours: float = 0,
        category: str = "telemetry",
    ) -> None:
        self.rows.append(
            {
                "id": len(self.rows) + 1,
                "ts": datetime.now(UTC) - timedelta(hours=ts_offset_hours),
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
        out = []
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
            out.append(r)  # type: ignore[arg-type]
        return out

    def query_events(self, **kwargs: Any) -> tuple[list[dict[str, Any]], bool]:
        rows = sorted(self._match(**kwargs), key=lambda r: r["ts"], reverse=True)
        limit = kwargs.get("limit", 100)
        offset = kwargs.get("offset", 0)
        page = rows[offset : offset + limit]
        return page, len(rows) > offset + limit

    def count_events(self, **kwargs: Any) -> int:
        return len(self._match(**kwargs))

    def attribute_aggregate(self, **kwargs: Any) -> Any:
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


@pytest.fixture(autouse=True)
def fake_loki(monkeypatch: pytest.MonkeyPatch) -> _FakeLoki:
    """Route all loki_events calls through an in-memory fake; each test gets
    an empty store and adds its own rows."""
    fake = _FakeLoki()
    monkeypatch.setattr(loki_events, "query_events", fake.query_events)
    monkeypatch.setattr(loki_events, "count_events", fake.count_events)
    monkeypatch.setattr(loki_events, "attribute_aggregate", fake.attribute_aggregate)
    return fake


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
    """cost accumulated since spawn —— 200h old llm_usage also counted (no time window),
    contrasting with the dashboard's 24h window. Lock field names: in_total/out_total/cache_read/reasoning."""
    aid = _insert_agent(db_conn)
    # claude-opus-4-8 (5, 5, 25) USD/M: in=1M out=1M cache=0 → 30.0, 200h ago
    fake_loki.add(
        event="llm_usage",
        agent_id=aid,
        payload={
            "in_total": 1_000_000,
            "out_total": 1_000_000,
            "cache_read": 0,
            "reasoning": 12345,
            "model": "claude-opus-4-8",
        },
        ts_offset_hours=200,
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


def test_inspect_whole_life_window_bounded_to_loki_limit(
    db_conn: psycopg.Connection, fake_loki: _FakeLoki
) -> None:
    """Regression (2026-08-12 prod inspector 500): whole-life queries used a
    far-past sentinel (2000-01-01); real Loki rejects windows older than its
    max_query_length guardrail (30d1h) with a 400, and the gateway surfaced
    it as a 500. The fake models that guardrail, and whole-life windows are
    now capped at `_whole_life_start()` (now − 30d): a no-hours inspect
    succeeds, and rows older than the cap (which real Loki would not even
    retain) fall outside the cumulative window."""
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


def _insert_event(
    db: psycopg.Connection,
    *,
    agent_id: int,
    event: str = "llm_usage",
    payload: dict | None = None,  # pyright: ignore[reportMissingTypeArgument, reportUnknownParameterType]
    ts_offset_hours: float = 0,
    category: str = "telemetry",
) -> None:
    """INSERT one event row into the PG `events` archive (the frozen
    pre-LGTM history the inspector now reads for the pre-cutover era)."""
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO events (ts, agent_id, level, event_name, attributes, "
            "machine, process, category, source) "
            "VALUES (now() - %s * interval '1 hour', %s, 'info', %s, %s::jsonb, "
            "'test', 'test', %s, 'test')",
            (ts_offset_hours, agent_id, event, json.dumps(payload or {}), category),
        )


def _archive_boundary_anchor(db_conn: psycopg.Connection, *, agent_id: int) -> None:
    """Pin the archive's freeze point to ~now: the inspector partitions the
    timeline at max(events.ts), so a test mixing archive-era rows (old ts)
    with Loki rows (ts≈now) must insert a boundary anchor — otherwise the
    oldest archive row would BE the boundary and sit outside its own window
    (`ts < boundary`). A turn_end row serves as the freeze marker."""
    _insert_event(
        db_conn,
        agent_id=agent_id,
        event="turn_end",
        payload={"ok": True, "duration_seconds": 1.0},
        ts_offset_hours=0.1,
    )


def test_inspect_archive_history_counts_in_whole_life(
    db_conn: psycopg.Connection, fake_loki: _FakeLoki
) -> None:
    """Task #1273 regression: the LGTM cutover made the inspector read Loki
    only, and Loki's storage was born at the cutover (~7d retention) — an
    agent's whole-life cost silently collapsed to the Loki window (405:
    ~$14.5 history shown as $0.74). The frozen PG `events` archive now serves
    the pre-cutover era: a 40d-old archive row (beyond Loki's reach entirely)
    counts, and the Loki row at ts≈now counts once — no double count across
    the boundary, both sides price the same model."""
    aid = _insert_agent(db_conn)
    # 40d old — real Loki neither retains it nor accepts the query window
    _insert_event(
        db_conn,
        agent_id=aid,
        payload={
            "in_total": 1_000_000,
            "out_total": 1_000_000,
            "cache_read": 0,
            "model": "claude-opus-4-8",
        },
        ts_offset_hours=40 * 24,
    )
    _archive_boundary_anchor(db_conn, agent_id=aid)
    # Loki-era row (ts≈now) — same model, priced identically
    fake_loki.add(
        event="llm_usage",
        agent_id=aid,
        payload={
            "in_total": 500_000,
            "out_total": 500_000,
            "cache_read": 0,
            "model": "claude-opus-4-8",
        },
    )
    db_conn.commit()
    with TestClient(app) as client:
        body = client.get(f"/api/agents/{aid}/inspect").json()
    cost = body["cost"]
    assert cost["llm_calls"] == 2
    assert cost["tokens_in"] == 1_500_000
    # (1M+0.5M in, 1M+0.5M out) at claude-opus-4-8 (5, .5, 25)/M → 30 + 15
    assert cost["cost_usd"] == pytest.approx(45.0)  # pyright: ignore[reportUnknownMemberType]
    assert cost["unpriced_calls"] == 0


def test_inspect_snapshot_cost_immune_to_registry_changes(
    db_conn: psycopg.Connection, fake_loki: _FakeLoki
) -> None:
    """User principle (task #1273): cost is billed at usage time — a row's
    stored cost_usd is summed as-is and NEVER re-priced against the current
    registry. Both rows carry snapshots whose values differ from what the
    registry would compute today; the response must equal the snapshots, not
    the registry math."""
    aid = _insert_agent(db_conn)
    _insert_event(
        db_conn,
        agent_id=aid,
        payload={
            "in_total": 1_000_000,
            "out_total": 1_000_000,
            "cache_read": 0,
            "model": "deepseek-v4-pro",
            "cost_usd": 50.0,
        },
        ts_offset_hours=3 * 24,
    )
    _archive_boundary_anchor(db_conn, agent_id=aid)
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
    assert cost["estimated_calls"] == 0


def test_inspect_legacy_rows_estimated_at_read_time(
    db_conn: psycopg.Connection, fake_loki: _FakeLoki
) -> None:
    """Rows written before the snapshot shipped carry no cost_usd; the read
    side prices exactly those rows via cost_usd (current registry + retired
    ledger) and counts them as estimated_calls — the honest marker for
    read-time-priced legacy rows. A mixed model: snapshot row + legacy row
    both count once, legacy priced on its own tokens only."""
    aid = _insert_agent(db_conn)
    _insert_event(
        db_conn,
        agent_id=aid,
        payload={
            "in_total": 1_000_000,
            "out_total": 0,
            "cache_read": 1_000_000,  # full cache hit → $0.003625
            "model": "deepseek-v4-pro",
            "cost_usd": 0.003625,
        },
        ts_offset_hours=1,
    )
    _archive_boundary_anchor(db_conn, agent_id=aid)
    # legacy row: in=1M, cached=1M → priced at read time: 1M * 0.003625 / 1M
    fake_loki.add(
        event="llm_usage",
        agent_id=aid,
        payload={
            "in_total": 1_000_000,
            "out_total": 0,
            "cache_read": 1_000_000,
            "model": "deepseek-v4-pro",
        },
    )
    db_conn.commit()
    with TestClient(app) as client:
        body = client.get(f"/api/agents/{aid}/inspect").json()
    cost = body["cost"]
    assert cost["llm_calls"] == 2
    assert cost["estimated_calls"] == 1
    assert cost["unpriced_calls"] == 0
    # the response rounds to 4dp (AgentCost); 0.003625+0.003625 floats to
    # 0.0072500...1 and rounds UP to 0.0073
    assert cost["cost_usd"] == pytest.approx(0.0073)  # pyright: ignore[reportUnknownMemberType]


def test_inspect_retired_model_priced_via_ledger(
    db_conn: psycopg.Connection, fake_loki: _FakeLoki
) -> None:
    """A model removed from the registry (gemini-3.6-flash, retired 2026-08-13)
    keeps its historical price via RETIRED_MODEL_PRICING — historical rows
    must not silently drop out of cost when a model retires. The Grafana
    registry-derived panels had exactly this bug (3.6 rows dropped); the
    inspector's legacy path prices them from the ledger."""
    aid = _insert_agent(db_conn)
    _archive_boundary_anchor(db_conn, agent_id=aid)
    fake_loki.add(
        event="llm_usage",
        agent_id=aid,
        payload={
            "in_total": 1_000_000,
            "out_total": 1_000_000,
            "cache_read": 0,
            "model": "gemini-3.6-flash",
        },
    )
    db_conn.commit()
    with TestClient(app) as client:
        body = client.get(f"/api/agents/{aid}/inspect").json()
    cost = body["cost"]
    # ledger (1.5, 0.15, 7.5)/M → 1.5 + 7.5
    assert cost["cost_usd"] == pytest.approx(9.0)  # pyright: ignore[reportUnknownMemberType]
    assert cost["estimated_calls"] == 1
    assert cost["unpriced_calls"] == 0


def test_inspect_hours_window_crosses_archive_boundary(
    db_conn: psycopg.Connection, fake_loki: _FakeLoki
) -> None:
    """An hours window that reaches into the archive era (here: the anchor
    pins the boundary at ~now, so the whole 168h window is archive-era)
    counts the archive rows — the window selector scopes the stitched
    timeline, not just Loki."""
    aid = _insert_agent(db_conn)
    _insert_event(
        db_conn,
        agent_id=aid,
        payload={
            "in_total": 1_000_000,
            "out_total": 1_000_000,
            "cache_read": 0,
            "model": "claude-opus-4-8",
        },
        ts_offset_hours=100,
    )
    _archive_boundary_anchor(db_conn, agent_id=aid)
    db_conn.commit()
    with TestClient(app) as client:
        body = client.get(f"/api/agents/{aid}/inspect?hours=168").json()
        body24 = client.get(f"/api/agents/{aid}/inspect?hours=24").json()
    cost = body["cost"]
    assert cost["llm_calls"] == 1
    assert cost["cost_usd"] == pytest.approx(30.0)  # pyright: ignore[reportUnknownMemberType]
    # 24h window: the 100h-old row is outside
    assert body24["cost"]["llm_calls"] == 0


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


def test_inspect_exec_ok_fail_split(db_conn: psycopg.Connection, fake_loki: _FakeLoki) -> None:
    """exec_ok = plain 'exec'; exec_failed = exec_failed / exec_thread_stuck /
    exec(timeout). Non-exec events like 'code' not counted."""
    aid = _insert_agent(db_conn)
    fake_loki.add(event="exec", agent_id=aid)
    fake_loki.add(event="exec", agent_id=aid)
    fake_loki.add(event="exec_failed", agent_id=aid)
    fake_loki.add(event="exec_thread_stuck", agent_id=aid)
    fake_loki.add(event="exec(timeout)", agent_id=aid)
    # non-exec event — must not pollute counts
    fake_loki.add(event="code", agent_id=aid)
    db_conn.commit()
    with TestClient(app) as client:
        body = client.get(f"/api/agents/{aid}/inspect").json()
    stats = body["stats"]
    assert stats["exec_ok"] == 2
    assert stats["exec_failed"] == 3


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
    """`?hours=24` narrows cost + stats to the last 24h: 25h old llm_usage/turn_end fall outside the window,
    1h old fall inside. Without hours both counted (cumulative). window_hours echoed back as-is."""
    aid = _insert_agent(db_conn)
    # 25h ago — outside the 24h window
    fake_loki.add(
        event="llm_usage",
        agent_id=aid,
        payload={
            "in_total": 1_000_000,
            "out_total": 1_000_000,
            "cache_read": 0,
            "model": "claude-opus-4-8",
        },
        ts_offset_hours=25,
    )
    fake_loki.add(
        event="turn_end",
        agent_id=aid,
        payload={"duration_seconds": 2.0, "ok": True},
        ts_offset_hours=25,
    )
    # 1h ago — inside the window
    fake_loki.add(
        event="llm_usage",
        agent_id=aid,
        payload={
            "in_total": 200_000,
            "out_total": 40_000,
            "cache_read": 0,
            "model": "claude-opus-4-8",
        },
        ts_offset_hours=1,
    )
    fake_loki.add(
        event="turn_end",
        agent_id=aid,
        payload={"duration_seconds": 4.0, "ok": True},
        ts_offset_hours=1,
    )
    db_conn.commit()
    with TestClient(app) as client:
        windowed = client.get(f"/api/agents/{aid}/inspect", params={"hours": 24}).json()
        cumulative = client.get(f"/api/agents/{aid}/inspect").json()
    # windowed: only the 1h row. opus-4-8 (5,5,25): 0.2M*5 + 0.04M*25 = 1.0 + 1.0 = 2.0
    assert windowed["window_hours"] == 24
    assert windowed["cost"]["llm_calls"] == 1
    assert windowed["cost"]["cost_usd"] == pytest.approx(2.0)  # pyright: ignore[reportUnknownMemberType]
    assert windowed["stats"]["turn_total"] == 1
    # cumulative: both rows. 25h: 1M*5 + 1M*25 = 30.0; 1h: 2.0 -> 32.0; turns 2
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
    # only the post-compact call: opus-4-8 (5,5,25): 0.2M*5 + 0.04M*25 = 2.0
    assert body["cost"]["llm_calls"] == 1
    assert body["cost"]["cost_usd"] == pytest.approx(2.0)  # pyright: ignore[reportUnknownMemberType]
    assert body["stats"]["turn_total"] == 1
    assert body["stats"]["turn_ok"] == 1


def test_inspect_since_compact_never_compacted_is_cumulative(
    db_conn: psycopg.Connection, fake_loki: _FakeLoki
) -> None:
    """Agent never compacted + since_compact=true → entire lifetime is in window."""
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
        ts_offset_hours=200,
    )
    db_conn.commit()
    with TestClient(app) as client:
        body = client.get(f"/api/agents/{aid}/inspect", params={"since_compact": "true"}).json()
    assert body["since_compact"] is True
    assert body["cost"]["llm_calls"] == 1
    assert body["cost"]["cost_usd"] == pytest.approx(30.0)  # pyright: ignore[reportUnknownMemberType]


@pytest.mark.parametrize("bad", ["0", "5", "-1", "169", "abc", "24.5"])
def test_inspect_invalid_hours_422(db_conn: psycopg.Connection, bad: str) -> None:
    """hours not in the whitelist {1,6,24,72,168} → 422 (fail-fast, reusing StatsWindowHours)."""
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
