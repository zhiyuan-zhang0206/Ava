"""GET /api/agents/{id}/neighbors integration tests.

FastAPI TestClient + real ava_test DB + FakeLoki. Task #180 (LGTM cutover):
the retired `agent_neighbors` SQL function read the frozen `events` table
and silently returned no peers; the walk now runs in Python over the event
stream (gateway/neighbors.py) — audit edge rows stitch the Loki archive stream
(task #1281, all pre-cutover events) with the live tail. Covered here end to
end: undirected ties, permanent lineage weights (spawn/fork/resurrect, no
time decay) vs decaying message weights (send_message, EXP(-k*days)),
per-hop gamma decay, terminated inclusion, limit, self/root exclusions, and
the archive+Loki merge on the same pair.
"""

import json as _json
import math
import threading
from datetime import timedelta
from typing import Any

import httpx
import psycopg
import pytest
from fastapi.testclient import TestClient

from gateway import loki_events, neighbors
from gateway.app import app
from shared.loki_index_labels import ARCHIVE_FREEZE_AT
from tests.gateway.loki_fake import FakeLoki


class _FakeRedis:
    """In-memory stand-in for the frozen-source cache's sync_redis client."""

    def __init__(self) -> None:
        self.store: dict[str, str] = {}
        self.writes: list[tuple[str, str, int | None]] = []

    def get(self, key: str) -> str | None:
        return self.store.get(key)

    def set(self, key: str, value: str, ex: int | None = None) -> None:
        self.store[key] = value
        self.writes.append((key, value, ex))

    def __enter__(self) -> "_FakeRedis":
        return self

    def __exit__(self, *_args: object) -> None:
        return None


@pytest.fixture(autouse=True)
def fake_loki(monkeypatch: pytest.MonkeyPatch) -> FakeLoki:
    """Route all loki_events calls through an in-memory fake; each test gets
    an empty store and adds its own rows."""
    fake = FakeLoki()
    monkeypatch.setattr(loki_events, "query_events", fake.query_events)
    monkeypatch.setattr(loki_events, "count_events", fake.count_events)
    return fake


@pytest.fixture(autouse=True)
def frozen_cache(monkeypatch: pytest.MonkeyPatch) -> _FakeRedis:
    """A fresh per-test fake Redis for the archive-row cache, so cached state
    never leaks between tests (and the real Redis is never touched)."""
    fake = _FakeRedis()

    def _fake_sync_redis(*_args: object, **_kwargs: object) -> _FakeRedis:
        return fake

    monkeypatch.setattr(neighbors, "sync_redis", _fake_sync_redis)
    return fake


def _seed_agent(
    db_conn: psycopg.Connection,
    *,
    status: str = "running",
    born_spawner: str | None = None,
) -> int:
    with db_conn.cursor() as cur:
        cur.execute("INSERT INTO agents DEFAULT VALUES RETURNING id")
        row = cur.fetchone()
        assert row is not None
        new_id = row[0]
        cur.execute(
            "INSERT INTO agents_meta (id, spawner, born_spawner, status) VALUES (%s, 'test', %s, %s)",
            (new_id, born_spawner, status),
        )
    db_conn.commit()
    return new_id


def _event(
    fake: FakeLoki,
    *,
    event_type: str,
    agent_id: int,
    target: int | None,
    days_ago: float = 0.0,
    count: int = 1,
) -> None:
    """Add `count` live-tail audit events for an (agent_id, target) pair at
    a fixed age. agent_id and target are the two endpoints of the
    inter-agent tie; the walk keys purely on them, so the source string is
    irrelevant to the graph."""
    for _ in range(count):
        fake.add(
            event=event_type,
            agent_id=agent_id,
            target_agent_id=target,
            category="audit",
            ts_offset_hours=days_ago * 24.0,
        )


def _archive_event(
    fake_loki: FakeLoki,
    *,
    event_type: str,
    agent_id: int,
    target: int | None,
    age_hours: float = 1.0,
) -> None:
    """Add one ARCHIVE-era audit event to the Loki fake's archive stream
    (ts `age_hours` before the ARCHIVE_FREEZE_AT constant — the task #1281
    archive stream holds only pre-cutover rows)."""
    fake_loki.add(
        event=event_type,
        agent_id=agent_id,
        target_agent_id=target,
        category="audit",
        ts=ARCHIVE_FREEZE_AT - timedelta(hours=age_hours),
        archive=True,
    )


def _neighbors(client: TestClient, agent_id: int, **params: int) -> list[dict]:
    resp = client.get(f"/api/agents/{agent_id}/neighbors", params=params)
    assert resp.status_code == 200, resp.text
    return resp.json()["neighbors"]


def test_direct_ties_both_directions_self_and_root_excluded(
    db_conn: psycopg.Connection, fake_loki: FakeLoki
) -> None:
    a = _seed_agent(db_conn)
    b = _seed_agent(db_conn)
    c = _seed_agent(db_conn)
    # b messaged a (tie a-b); a spawned c (tie a-c). Direction does not matter.
    _event(fake_loki, event_type="send_message", agent_id=b, target=a)
    _event(fake_loki, event_type="spawn", agent_id=c, target=a)

    with TestClient(app) as client:
        rows = _neighbors(client, a, depth=1)

    ids = {r["agent_id"] for r in rows}
    assert ids == {b, c}  # root a excluded; both neighbors found regardless of direction
    assert all(r["depth"] == 1 for r in rows)  # pyright: ignore[reportUnknownArgumentType]


def test_lineage_and_message_equal_at_zero_age(
    db_conn: psycopg.Connection, fake_loki: FakeLoki
) -> None:
    a = _seed_agent(db_conn)
    b = _seed_agent(db_conn)
    c = _seed_agent(db_conn)
    # At age 0 the message decay factor is EXP(0) == 1, so a fresh spawn tie
    # (permanent LN(1+count)) and a fresh message tie (EXP(0)*LN(1+count)) coincide.
    _event(fake_loki, event_type="spawn", agent_id=b, target=a, days_ago=0.0)
    _event(fake_loki, event_type="send_message", agent_id=c, target=a, days_ago=0.0)

    with TestClient(app) as client:
        rows = _neighbors(client, a, depth=1)

    by_id = {r["agent_id"]: r for r in rows}
    assert by_id[b]["score"] == by_id[c]["score"]


def test_lineage_permanent_message_decays_over_time(
    db_conn: psycopg.Connection, fake_loki: FakeLoki
) -> None:
    a = _seed_agent(db_conn)
    lineage = _seed_agent(db_conn)
    msg = _seed_agent(db_conn)
    # Both ties are 5 days old with the same count. The lineage (spawn) weight
    # does not decay; the message weight does -> lineage now outranks the message.
    _event(fake_loki, event_type="spawn", agent_id=lineage, target=a, days_ago=5.0)
    _event(fake_loki, event_type="send_message", agent_id=msg, target=a, days_ago=5.0)

    with TestClient(app) as client:
        rows = _neighbors(client, a, depth=1)

    by_id = {r["agent_id"]: r for r in rows}
    assert set(by_id) == {lineage, msg}  # pyright: ignore[reportUnknownArgumentType]
    assert by_id[lineage]["score"] > by_id[msg]["score"]


def test_resurrect_counts_as_a_tie(db_conn: psycopg.Connection, fake_loki: FakeLoki) -> None:
    a = _seed_agent(db_conn)
    b = _seed_agent(db_conn)
    _event(fake_loki, event_type="resurrect", agent_id=b, target=a)

    with TestClient(app) as client:
        rows = _neighbors(client, a, depth=1)

    assert {r["agent_id"] for r in rows} == {b}


def test_recency_decay_ranks_recent_first(db_conn: psycopg.Connection, fake_loki: FakeLoki) -> None:
    a = _seed_agent(db_conn)
    recent = _seed_agent(db_conn)
    stale = _seed_agent(db_conn)
    _event(fake_loki, event_type="send_message", agent_id=recent, target=a, days_ago=0.0)
    _event(fake_loki, event_type="send_message", agent_id=stale, target=a, days_ago=10.0)

    with TestClient(app) as client:
        rows = _neighbors(client, a, depth=1)

    assert [r["agent_id"] for r in rows] == [recent, stale]
    assert rows[0]["score"] > rows[1]["score"]


def test_depth_limits_reach_and_gamma_decays_deeper_hops(
    db_conn: psycopg.Connection, fake_loki: FakeLoki
) -> None:
    a = _seed_agent(db_conn)
    b = _seed_agent(db_conn)
    c = _seed_agent(db_conn)
    # Chain a - b - c, identical fresh edges. c is two hops from a.
    _event(fake_loki, event_type="send_message", agent_id=b, target=a, days_ago=0.0)
    _event(fake_loki, event_type="send_message", agent_id=c, target=b, days_ago=0.0)

    with TestClient(app) as client:
        depth1 = _neighbors(client, a, depth=1)
        depth2 = _neighbors(client, a, depth=2)

    # depth=1 sees only the direct neighbor b.
    assert {r["agent_id"] for r in depth1} == {b}
    # depth=2 reaches c, marked as a 2-hop neighbor, and below b (gamma discount).
    by_id = {r["agent_id"]: r for r in depth2}
    assert set(by_id) == {b, c}  # pyright: ignore[reportUnknownArgumentType]
    assert by_id[b]["depth"] == 1
    assert by_id[c]["depth"] == 2
    assert by_id[c]["score"] < by_id[b]["score"]


def test_terminated_neighbor_included_with_status(
    db_conn: psycopg.Connection, fake_loki: FakeLoki
) -> None:
    a = _seed_agent(db_conn)
    dead = _seed_agent(db_conn, status="terminated")
    _event(fake_loki, event_type="send_message", agent_id=dead, target=a)

    with TestClient(app) as client:
        rows = _neighbors(client, a, depth=1)

    assert len(rows) == 1  # pyright: ignore[reportUnknownArgumentType]
    assert rows[0]["agent_id"] == dead
    assert rows[0]["status"] == "terminated"


def test_limit_caps_result_count(db_conn: psycopg.Connection, fake_loki: FakeLoki) -> None:
    a = _seed_agent(db_conn)
    for _ in range(5):
        peer = _seed_agent(db_conn)
        _event(fake_loki, event_type="send_message", agent_id=peer, target=a)

    with TestClient(app) as client:
        rows = _neighbors(client, a, depth=1, limit=2)

    assert len(rows) == 2  # pyright: ignore[reportUnknownArgumentType]


def test_no_ties_returns_empty(db_conn: psycopg.Connection, fake_loki: FakeLoki) -> None:
    a = _seed_agent(db_conn)
    with TestClient(app) as client:
        rows = _neighbors(client, a, depth=1)
    assert rows == []


def test_unknown_agent_404(db_conn: psycopg.Connection, fake_loki: FakeLoki) -> None:
    with TestClient(app) as client:
        resp = client.get("/api/agents/999999/neighbors")
    assert resp.status_code == 404


def test_depth_out_of_range_422(db_conn: psycopg.Connection, fake_loki: FakeLoki) -> None:
    a = _seed_agent(db_conn)
    with TestClient(app) as client:
        assert client.get(f"/api/agents/{a}/neighbors", params={"depth": 0}).status_code == 422
        assert client.get(f"/api/agents/{a}/neighbors", params={"depth": 6}).status_code == 422


def test_telemetry_message_does_not_create_neighbor(
    db_conn: psycopg.Connection, fake_loki: FakeLoki
) -> None:
    """The neighbor traversal reads category='audit' only: a send_message row
    written with category='telemetry' (a mislabeled write) must not produce a
    tie — the graph edge family is audit-only by contract."""
    a = _seed_agent(db_conn)
    b = _seed_agent(db_conn)
    fake_loki.add(
        event="send_message",
        agent_id=b,
        target_agent_id=a,
        category="telemetry",
        ts_offset_hours=24.0,
    )

    with TestClient(app) as client:
        rows = _neighbors(client, a, depth=1)
    assert rows == []


def test_archive_and_loki_merge_on_the_same_pair(
    db_conn: psycopg.Connection, fake_loki: FakeLoki
) -> None:
    """The two sides partition the timeline at the freeze boundary and sum
    exactly like one table: counts add before the LN, so one archive message
    + one live message weigh LN(1+2) — not 2 * LN(2)."""
    a = _seed_agent(db_conn)
    b = _seed_agent(db_conn)
    _archive_event(fake_loki, event_type="send_message", agent_id=b, target=a, age_hours=1.0)
    _event(fake_loki, event_type="send_message", agent_id=b, target=a, days_ago=0.0)

    with TestClient(app) as client:
        rows = _neighbors(client, a, depth=1)

    assert len(rows) == 1  # pyright: ignore[reportUnknownArgumentType]
    score = rows[0]["score"]
    assert score == pytest.approx(math.log1p(2), abs=1e-3)  # pyright: ignore[reportUnknownMemberType]


def test_archive_lineage_alone_forms_a_tie(
    db_conn: psycopg.Connection, fake_loki: FakeLoki
) -> None:
    """A pre-cutover lineage tie lives in the frozen archive only — the walk
    must still reach it (the archive read is deliberate, not dropped)."""
    a = _seed_agent(db_conn)
    b = _seed_agent(db_conn)
    _archive_event(fake_loki, event_type="spawn", agent_id=b, target=a, age_hours=2.0)

    with TestClient(app) as client:
        rows = _neighbors(client, a, depth=1)

    assert {r["agent_id"] for r in rows} == {b}
    assert rows[0]["score"] == pytest.approx(math.log1p(1), abs=1e-3)  # pyright: ignore[reportUnknownMemberType]


# ── ancestors: the immutable birth chain above the queried agent ─────────


def _ancestors(client: TestClient, agent_id: int, **params: int) -> list[dict]:
    resp = client.get(f"/api/agents/{agent_id}/neighbors", params=params)
    assert resp.status_code == 200, resp.text
    return resp.json()["ancestors"]


def test_ancestors_spawn_chain_nearest_first_walks_to_top(db_conn: psycopg.Connection) -> None:
    """a births b, b births c: c's ancestors are [b, a], nearest first."""
    a = _seed_agent(db_conn)
    b = _seed_agent(db_conn, born_spawner=f"agent:{a}")
    c = _seed_agent(db_conn, born_spawner=f"agent:{b}")

    with TestClient(app) as client:
        rows = _ancestors(client, c)
        # the parent is not an ancestor of itself — the chain is read correctly
        rows_b = _ancestors(client, b)

    assert [r["agent_id"] for r in rows] == [b, a]
    assert [r["depth"] for r in rows] == [1, 2]
    # each hop's edge is the permanent lineage weight, gamma-discounted per hop
    assert rows[0]["score"] == pytest.approx(math.log1p(1), abs=1e-3)  # pyright: ignore[reportUnknownMemberType]
    assert rows[1]["score"] < rows[0]["score"]
    assert [r["agent_id"] for r in rows_b] == [a]


def test_ancestors_ignore_neighbor_depth_param(db_conn: psycopg.Connection) -> None:
    """`depth` bounds the neighbor walk only; the ancestor chain always walks
    to the top — responsibility attribution needs the whole chain."""
    a = _seed_agent(db_conn)
    b = _seed_agent(db_conn, born_spawner=f"agent:{a}")
    c = _seed_agent(db_conn, born_spawner=f"agent:{b}")

    with TestClient(app) as client:
        rows = _ancestors(client, c, depth=1)

    assert [r["agent_id"] for r in rows] == [b, a]


def test_ancestors_fork_forms_a_parent(db_conn: psycopg.Connection) -> None:
    a = _seed_agent(db_conn)
    b = _seed_agent(db_conn, born_spawner=f"agent:{a}")

    with TestClient(app) as client:
        rows = _ancestors(client, b)

    assert [r["agent_id"] for r in rows] == [a]


def test_ancestors_message_ties_and_resurrect_never_parent(
    db_conn: psycopg.Connection, fake_loki: FakeLoki
) -> None:
    """Message and resurrect ties do not form ancestors without born_spawner."""
    a = _seed_agent(db_conn)
    b = _seed_agent(db_conn)
    _event(fake_loki, event_type="send_message", agent_id=b, target=a)
    _event(fake_loki, event_type="resurrect", agent_id=b, target=a)

    with TestClient(app) as client:
        assert _ancestors(client, a) == []


def test_ancestors_no_spawner_returns_empty(db_conn: psycopg.Connection) -> None:
    a = _seed_agent(db_conn)
    with TestClient(app) as client:
        assert _ancestors(client, a) == []


def test_ancestors_terminated_ancestor_included_with_status(db_conn: psycopg.Connection) -> None:
    """A terminated parent stays in the chain (same inclusion rule as
    neighbors) and carries its status."""
    a = _seed_agent(db_conn, status="terminated")
    b = _seed_agent(db_conn, born_spawner=f"agent:{a}")

    with TestClient(app) as client:
        rows = _ancestors(client, b)

    assert len(rows) == 1  # pyright: ignore[reportUnknownArgumentType]
    assert rows[0]["agent_id"] == a
    assert rows[0]["status"] == "terminated"


def test_ancestors_read_born_spawner_not_loki_parentage(
    db_conn: psycopg.Connection, fake_loki: FakeLoki
) -> None:
    """Ancestor lineage is independent of both live and archived Loki rows."""
    a = _seed_agent(db_conn)
    stale_event_parent = _seed_agent(db_conn)
    b = _seed_agent(db_conn, born_spawner=f"agent:{a}")
    _archive_event(
        fake_loki, event_type="spawn", agent_id=b, target=stale_event_parent, age_hours=2.0
    )
    _event(fake_loki, event_type="spawn", agent_id=b, target=stale_event_parent)

    with TestClient(app) as client:
        rows = _ancestors(client, b)

    assert [r["agent_id"] for r in rows] == [a]
    assert rows[0]["score"] == pytest.approx(math.log1p(1), abs=1e-3)  # pyright: ignore[reportUnknownMemberType]


def test_ancestors_use_one_constant_weight_per_birth_edge(db_conn: psycopg.Connection) -> None:
    """born_spawner has one parent per child, independent of event counts."""
    a = _seed_agent(db_conn)
    b = _seed_agent(db_conn, born_spawner=f"agent:{a}")

    with TestClient(app) as client:
        rows = _ancestors(client, b)

    assert [r["agent_id"] for r in rows] == [a]
    assert rows[0]["score"] == pytest.approx(math.log1p(1), abs=1e-3)  # pyright: ignore[reportUnknownMemberType]


# ── frozen archive cache: the immutable Loki archive is read once a day ──


def test_archive_rows_fetched_once_then_served_from_cache(
    db_conn: psycopg.Connection,
    fake_loki: FakeLoki,
    frozen_cache: _FakeRedis,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The archive stream is immutable, so its rows are cached: the first
    request runs the Loki archive query, the second serves the same answer
    without touching Loki again (task #1958 — the per-request archive scan
    was ~5-28s and is what made the route slow)."""
    a = _seed_agent(db_conn)
    b = _seed_agent(db_conn)
    _archive_event(fake_loki, event_type="spawn", agent_id=b, target=a, age_hours=2.0)

    archive_calls: list[dict[str, object]] = []
    original = fake_loki.query_events

    def counting_query(**kwargs: object) -> tuple[list[dict[str, Any]], bool]:
        if kwargs.get("archive"):
            archive_calls.append(kwargs)
        return original(**kwargs)

    monkeypatch.setattr(loki_events, "query_events", counting_query)

    with TestClient(app) as client:
        first = _neighbors(client, a, depth=1)
        second = _neighbors(client, a, depth=1)

    assert first == second
    assert {r["agent_id"] for r in first} == {b}
    assert len(archive_calls) == 1  # second request hit the Redis cache
    assert neighbors._ARCHIVE_CACHE_KEY in frozen_cache.store
    # the originating fetch's has_more rides the payload, so a cache hit
    # reports truncation exactly as the fetch did
    assert _json.loads(frozen_cache.store[neighbors._ARCHIVE_CACHE_KEY])["has_more"] is False


def test_archive_cache_redis_outage_degrades_to_direct_query(
    db_conn: psycopg.Connection, fake_loki: FakeLoki, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fail-open: when Redis is unavailable the route still answers, reading
    the archive straight from Loki (and skipping the cache write)."""
    a = _seed_agent(db_conn)
    b = _seed_agent(db_conn)
    _archive_event(fake_loki, event_type="spawn", agent_id=b, target=a, age_hours=2.0)

    class _BrokenRedis:
        def __enter__(self) -> "_BrokenRedis":
            raise RuntimeError("redis down")

        def __exit__(self, *_args: object) -> None:
            return None

    def _broken_sync_redis(*_args: object, **_kwargs: object) -> _BrokenRedis:
        return _BrokenRedis()

    monkeypatch.setattr(neighbors, "sync_redis", _broken_sync_redis)

    with TestClient(app) as client:
        rows = _neighbors(client, a, depth=1)

    assert {r["agent_id"] for r in rows} == {b}


def test_archive_refresh_failure_degrades_to_live_only_and_heals(
    db_conn: psycopg.Connection,
    fake_loki: FakeLoki,
    frozen_cache: _FakeRedis,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The daily archive refresh keeps the 45s default timeout, but Loki can
    still fail it: the route degrades to live-only ties (no 500), a short
    negative cache entry absorbs the retry storm instead of leaving the
    cache empty for every poll to re-attempt, and the first request after
    the negative entry is gone repopulates the real rows (2026-08-29/30
    incident: an empty cache + saturated Loki re-ran the scan on every
    request and 45s-timed out for hours)."""
    a = _seed_agent(db_conn)
    b = _seed_agent(db_conn)
    _archive_event(fake_loki, event_type="spawn", agent_id=b, target=a, age_hours=2.0)

    state = {"fail_next_archive": True}
    archive_calls = 0
    original = fake_loki.query_events

    def counting_flaky_query(**kwargs: object) -> tuple[list[dict[str, Any]], bool]:
        nonlocal archive_calls
        if kwargs.get("archive"):
            archive_calls += 1
        if kwargs.get("archive") and state["fail_next_archive"]:
            state["fail_next_archive"] = False
            raise httpx.ReadTimeout("timed out")
        return original(**kwargs)

    monkeypatch.setattr(loki_events, "query_events", counting_flaky_query)

    with TestClient(app) as client:
        degraded_resp = client.get(f"/api/agents/{a}/neighbors")
        assert degraded_resp.status_code == 200
        degraded_body = degraded_resp.json()
        # live-only: the archive tie is absent, the route answers instead of
        # 500ing, and the response says the archive read degraded
        assert degraded_body["neighbors"] == []
        assert degraded_body["degraded"] is True
        # the failed fetch wrote the short negative cache (degraded marker + 60s TTL)
        cached = _json.loads(frozen_cache.store[neighbors._ARCHIVE_CACHE_KEY])
        assert cached == {"rows": [], "has_more": False, "degraded": True}
        assert frozen_cache.writes[-1][2] == neighbors._NEGATIVE_CACHE_TTL_SECONDS
        # within the negative window every request hits the cache — no re-fetch
        again = client.get(f"/api/agents/{a}/neighbors")
        assert again.json()["neighbors"] == []
        assert again.json()["degraded"] is True
        assert archive_calls == 1
        # once the negative entry expires (simulated), the next request
        # refetches and repopulates the real rows
        del frozen_cache.store[neighbors._ARCHIVE_CACHE_KEY]
        rows = _neighbors(client, a, depth=1)

    assert {r["agent_id"] for r in rows} == {b}
    assert archive_calls == 2
    healed = _json.loads(frozen_cache.store[neighbors._ARCHIVE_CACHE_KEY])
    assert healed["rows"] and "degraded" not in healed
    assert frozen_cache.writes[-1][2] == neighbors._FROZEN_CACHE_TTL_SECONDS


def test_archive_fetch_single_flight_concurrent_misses_run_one_scan(
    fake_loki: FakeLoki, frozen_cache: _FakeRedis, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Concurrent cache misses run ONE whole-archive scan: a waiter that
    cannot enter the fetch within the bounded wait serves live-only ties
    instead of stacking its own scan on Loki (the stampede that saturated
    the querier during the 2026-08-29/30 incident)."""
    started = threading.Event()
    release = threading.Event()
    archive_calls = 0
    original = fake_loki.query_events

    def slow_archive_query(**kwargs: object) -> tuple[list[dict[str, Any]], bool]:
        nonlocal archive_calls
        if kwargs.get("archive"):
            archive_calls += 1
            started.set()
            release.wait(timeout=10)
        return original(**kwargs)

    monkeypatch.setattr(loki_events, "query_events", slow_archive_query)

    results: list[tuple[list[dict[str, Any]], bool]] = []
    errors: list[BaseException] = []

    def worker() -> None:
        try:
            results.append(neighbors._fetch_archive_rows())
        except BaseException as exc:  # pragma: no cover - failure reporting
            errors.append(exc)

    first = threading.Thread(target=worker)
    first.start()
    assert started.wait(timeout=5)  # the first thread holds the fetch lock
    second = threading.Thread(target=worker)
    second.start()
    second.join(timeout=10)
    assert not second.is_alive()  # the waiter gave up after the bounded wait
    release.set()
    first.join(timeout=10)
    assert not errors
    assert archive_calls == 1  # one scan for two concurrent misses
    # completion order is not deterministic (the waiter finishes first), so
    # compare as a set: one holder ran the (empty-archive) scan, one waiter
    # degraded after the bounded wait
    assert sorted(results, key=lambda t: t[1]) == [([], False), ([], True)]


def test_corrupt_archive_cache_entry_refetches_from_loki(
    db_conn: psycopg.Connection, fake_loki: FakeLoki, frozen_cache: _FakeRedis
) -> None:
    """A corrupt cached entry is a miss, not an answer: the decode failure
    refetches the archive instead of serving a wrong (or crashing) result."""
    a = _seed_agent(db_conn)
    b = _seed_agent(db_conn)
    _archive_event(fake_loki, event_type="spawn", agent_id=b, target=a, age_hours=2.0)
    # Valid JSON, wrong row shape (row[3] missing) — exercises the row decode.
    frozen_cache.store[neighbors._ARCHIVE_CACHE_KEY] = '{"rows": [[999, 998, "spawn"]]}'

    with TestClient(app) as client:
        rows = _neighbors(client, a, depth=1)

    assert {r["agent_id"] for r in rows} == {b}


def test_live_tail_read_is_bounded_and_cached_parts_never_requery(
    db_conn: psycopg.Connection, fake_loki: FakeLoki, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The live read is the only per-request Loki query and carries the 8s
    bound (the fleet graph's telemetry-read bound) instead of the shared
    client's 45s default, which is what pinned requests when Loki stalled."""
    live_calls: list[dict[str, object]] = []
    original = fake_loki.query_events

    def counting_query(**kwargs: object) -> tuple[list[dict[str, Any]], bool]:
        if not kwargs.get("archive"):
            live_calls.append(kwargs)
        return original(**kwargs)

    monkeypatch.setattr(loki_events, "query_events", counting_query)
    with TestClient(app) as _client:
        neighbors.compute(root=1, max_depth=1, limit=20, db_pool=app.state.db_pool)

    assert len(live_calls) == 1
    call = live_calls[0]
    assert call["timeout_s"] == neighbors._LIVE_READ_TIMEOUT_S
    # `from_` is the bare ARCHIVE_FREEZE_AT constant (gateway/neighbors.py
    # `_fetch_loki_edges`), never folded against now — only `to=now` moves. The
    # TestClient above just starts the app lifespan for `db_pool`; no request.
    assert call["from_"] == ARCHIVE_FREEZE_AT  # time-bomb-ok: constant floor, no clock fold


# ── migration round-trip: the down migration must actually execute ────────
