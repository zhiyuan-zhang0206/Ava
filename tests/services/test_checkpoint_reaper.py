"""`services.events_maintenance.checkpoint_reaper` — checkpoint retention.

Rule B (`reap_stale_checkpoints`): every STALE thread — an agent whose status
is 'terminated', or a live agent inactive for `_INACTIVE_HOURS` — is trimmed to
the latest 1 checkpoint (cascading writes + orphan blobs), bounding the
PostgresSaver's append-only growth that caused the 2026-08-08 disk crisis (21GB
of blobs in ~12h of terminated-agent accumulation). Rule A
(`trim_overgrown_threads`): ACTIVE threads past `_LIVE_TRIM_THRESHOLD`
checkpoints are trimmed to `_LIVE_KEEP`, replacing the removed agent-side idle
trim (which only fired when an agent actually idled). Pins: terminated and
inactive threads trimmed to keep=1; active agents never touched by Rule B;
overgrown active threads bounded by Rule A; the resurrect race (status /
last_active_at flipped between the candidate scan and the trim) skips instead
of reaping; already-trimmed threads are not candidates (idempotent); the
surviving checkpoint's blob stays (a full-snapshot channel may reference a
blob written long ago); aggregates are exact; the sync trim helper matches the
async one's counts.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from itertools import pairwise
from typing import Any, cast

import psycopg
import pytest
from psycopg_pool import ConnectionPool

import services.events_maintenance.checkpoint_reaper as reaper
from services.events_maintenance.checkpoint_reaper import (
    ReapCounts,
    reap_stale_checkpoints,
    trim_overgrown_threads,
)
from shared.checkpoint_cleanup import TrimCounts, trim_checkpoints_sync

_SCHEMA = """
CREATE TABLE checkpoints (
    thread_id            TEXT NOT NULL,
    checkpoint_ns        TEXT NOT NULL DEFAULT '',
    checkpoint_id        TEXT NOT NULL,
    parent_checkpoint_id TEXT,
    type                 TEXT,
    checkpoint           JSONB NOT NULL,
    metadata             JSONB NOT NULL DEFAULT '{}',
    PRIMARY KEY (thread_id, checkpoint_ns, checkpoint_id)
);
CREATE TABLE checkpoint_blobs (
    thread_id     TEXT NOT NULL,
    checkpoint_ns TEXT NOT NULL DEFAULT '',
    channel       TEXT NOT NULL,
    version       TEXT NOT NULL,
    type          TEXT NOT NULL,
    blob          BYTEA,
    PRIMARY KEY (thread_id, checkpoint_ns, channel, version)
);
CREATE TABLE checkpoint_writes (
    thread_id     TEXT NOT NULL,
    checkpoint_ns TEXT NOT NULL DEFAULT '',
    checkpoint_id TEXT NOT NULL,
    task_id       TEXT NOT NULL,
    idx           INTEGER NOT NULL,
    channel       TEXT NOT NULL,
    type          TEXT,
    blob          BYTEA NOT NULL,
    task_path     TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (thread_id, checkpoint_ns, checkpoint_id, task_id, idx)
);
CREATE TABLE agents_meta (
    id             BIGINT PRIMARY KEY,
    status         TEXT NOT NULL,
    last_active_at TIMESTAMPTZ DEFAULT now()
);
"""


@pytest.fixture
def pool(db_conn: psycopg.Connection) -> Iterator[ConnectionPool[Any]]:
    """A pool to a throwaway DB carrying the checkpoint + agents_meta tables."""
    _ = db_conn
    from tests.services.test_events_maintenance_ttl import _throwaway_db

    gen = cast(Any, _throwaway_db())
    url = next(gen)  # keep the generator alive — GC would run its finally (DROP DATABASE)
    try:
        with psycopg.connect(url, autocommit=True) as setup:
            setup.execute(_SCHEMA)  # type: ignore[arg-type]  # trusted multi-statement setup
        with ConnectionPool(url, min_size=1, max_size=2, open=True) as p:
            yield p
    finally:
        gen.close()


def _agent(
    pool: ConnectionPool[Any],
    agent_id: int,
    status: str,
    *,
    last_active_at: datetime | None = None,
) -> None:
    """Insert an agent; last_active_at None = DEFAULT now() (fresh/active)."""
    with pool.connection() as conn, conn.cursor() as cur:
        if last_active_at is None:
            cur.execute(
                "INSERT INTO agents_meta (id, status) VALUES (%s, %s)",
                (agent_id, status),
            )
        else:
            cur.execute(
                "INSERT INTO agents_meta (id, status, last_active_at) VALUES (%s, %s, %s)",
                (agent_id, status, last_active_at),
            )


def _thread(
    pool: ConnectionPool[Any],
    thread: str,
    n: int,
    *,
    ns: str = "",
    channel_version: str = "v1",
    boundary_at: int | None = None,
) -> None:
    """Insert `n` checkpoints for one thread, each with a write + a shared blob
    (the blob's version is referenced by every checkpoint's channel_versions —
    the exact "old blob referenced by the survivor" shape trim must keep).
    `boundary_at` (0-based index) marks that checkpoint as a compaction
    boundary."""
    with pool.connection() as conn, conn.cursor() as cur:
        for i in range(n):
            cid = f"ckpt-{thread}-{i:03d}"
            metadata = '{"compact_boundary": true}' if i == boundary_at else "{}"
            cur.execute(
                "INSERT INTO checkpoints (thread_id, checkpoint_ns, checkpoint_id, "
                "checkpoint, metadata) VALUES (%s, %s, %s, %s, %s)",
                (
                    thread,
                    ns,
                    cid,
                    f'{{"channel_versions": {{"messages": "{channel_version}"}}}}',
                    metadata,
                ),
            )
            cur.execute(
                "INSERT INTO checkpoint_writes (thread_id, checkpoint_ns, checkpoint_id, "
                "task_id, idx, channel, blob) VALUES (%s, %s, %s, 't', 0, 'messages', %s)",
                (thread, ns, cid, f"write-{thread}-{i}".encode()),
            )
        cur.execute(
            "INSERT INTO checkpoint_blobs (thread_id, checkpoint_ns, channel, version, "
            "type, blob) VALUES (%s, %s, 'messages', %s, 'json', %s)",
            (thread, ns, channel_version, f"blob-{thread}".encode()),
        )


def _count(pool: ConnectionPool[Any], table: str, thread: str | None = None) -> int:
    with pool.connection() as conn, conn.cursor() as cur:
        if thread is None:
            cur.execute(f"SELECT count(*) FROM {table}")  # noqa: S608 — table is a test-internal constant
        else:
            cur.execute(
                f"SELECT count(*) FROM {table} WHERE thread_id = %s",  # noqa: S608 — table is a test-internal constant
                (thread,),
            )
        return cur.fetchone()[0]


def test_rotation_window_advances_per_minute_and_is_idempotent() -> None:
    """Direct lock on the rotation window (QA #3242, 2026-08-25): with a
    CONSTANT candidate set (the starvation shape — heavy writers re-grow past
    the threshold within a pass), head-only consumption would never reach the
    tail. The window must advance by the pass size every minute and be
    idempotent within a minute."""
    candidates = list(range(1, 25))
    windows = [
        reaper._rotate_candidates(candidates, max_agents=8, now_seconds=now)
        for now in (1200.0, 1260.0, 1320.0)
    ]

    # Same minute -> same window (no state, no drift).
    assert windows[0] == reaper._rotate_candidates(candidates, max_agents=8, now_seconds=1200.0)
    # Each window is the full candidate set, exactly once (a rotation).
    for window in windows:
        assert len(window) == 24 and set(window) == set(candidates)
    # Consecutive minutes advance the window start by the pass size (mod n).
    offsets = [candidates.index(window[0]) for window in windows]
    assert [(b - a) % 24 for a, b in pairwise(offsets)] == [8, 8]
    # Coverable within ceil(24/8) consecutive windows.
    covered: set[int] = set()
    for window in windows:
        covered |= set(window)
    assert covered == set(candidates)


def test_rotation_reaches_all_overgrown_threads(
    pool: ConnectionPool[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A burst of overgrown active threads is spread over passes by the rotation
    window: every thread is trimmed within ceil(n/cap) passes, not only the first
    8 by agent id (2026-08-24 starvation: threads 3340/3341/3343 with 774/542/186
    checkpoints were never reached while the head of the id-ascending list was
    trimmed every fast loop)."""
    from types import SimpleNamespace

    for aid in range(401, 425):  # 24 active, over threshold
        _agent(pool, aid, "running")
        _thread(pool, str(aid), 25)

    # Freeze the wall clock at one minute so the rotation window is stable
    # across the three passes below (each pass trims the next 8 candidates).
    monkeypatch.setattr(
        "services.events_maintenance.checkpoint_reaper.time",
        SimpleNamespace(time=lambda: 20 * 60.0),
    )

    counts = trim_overgrown_threads(pool)
    assert counts.agents == 8
    counts = trim_overgrown_threads(pool)
    assert counts.agents == 8
    counts = trim_overgrown_threads(pool)
    assert counts.agents == 8

    for aid in range(401, 425):
        assert _count(pool, "checkpoints", str(aid)) == 2  # _LIVE_KEEP


def test_reaps_terminated_threads_to_keep_one(pool: ConnectionPool[Any]) -> None:
    _agent(pool, 101, "terminated")
    _thread(pool, "101", 5)

    counts = reap_stale_checkpoints(pool)

    assert counts == ReapCounts(agents=1, checkpoints=4, writes=4, blobs=0)
    assert _count(pool, "checkpoints", "101") == 1
    assert _count(pool, "checkpoint_writes", "101") == 1
    # the surviving checkpoint still references the shared blob -> it must stay
    assert _count(pool, "checkpoint_blobs", "101") == 1


def test_reaps_inactive_live_threads_to_keep_one(pool: ConnectionPool[Any]) -> None:
    """Rule B also covers live agents that have not completed a turn in
    `_INACTIVE_HOURS` (last_active_at stale) — an inactive agent is either
    dead weight or, on waking, fully restorable from the single newest
    checkpoint."""
    _agent(
        pool,
        151,
        "idling",
        last_active_at=datetime.now(UTC) - timedelta(hours=48),
    )
    _thread(pool, "151", 5)

    counts = reap_stale_checkpoints(pool)

    assert counts == ReapCounts(agents=1, checkpoints=4, writes=4, blobs=0)
    assert _count(pool, "checkpoints", "151") == 1


def test_never_touches_active_live_agents(pool: ConnectionPool[Any]) -> None:
    _agent(pool, 201, "running")
    _agent(pool, 202, "idling")
    _thread(pool, "201", 6)
    _thread(pool, "202", 4)

    counts = reap_stale_checkpoints(pool)

    assert counts == ReapCounts(agents=0, checkpoints=0, writes=0, blobs=0)
    assert _count(pool, "checkpoints") == 10


def test_rule_a_trims_overgrown_active_threads(pool: ConnectionPool[Any]) -> None:
    """Rule A bounds active threads past the threshold — the service-side
    replacement for the removed agent-side idle trim (which never fired for
    continuously working agents)."""
    _agent(pool, 251, "running")
    _thread(pool, "251", 25)

    counts = trim_overgrown_threads(pool)

    assert counts == ReapCounts(agents=1, checkpoints=23, writes=23, blobs=0)
    assert _count(pool, "checkpoints", "251") == 2


def test_rule_a_skips_under_threshold_and_stale(pool: ConnectionPool[Any]) -> None:
    _agent(pool, 261, "running")
    _thread(pool, "261", 15)  # under threshold -> not a Rule A candidate
    _agent(pool, 262, "terminated")
    _thread(pool, "262", 25)  # stale -> Rule B's keep=1, never Rule A's keep=2

    counts = trim_overgrown_threads(pool)

    assert counts == ReapCounts(agents=0, checkpoints=0, writes=0, blobs=0)
    assert _count(pool, "checkpoints", "261") == 15
    assert _count(pool, "checkpoints", "262") == 25


def test_keeps_compaction_boundaries(pool: ConnectionPool[Any]) -> None:
    """A checkpoint stamped `compact_boundary` survives any trim regardless of
    age — each past compaction segment stays recoverable (Task #1125)."""
    _agent(pool, 271, "terminated")
    _thread(pool, "271", 8, boundary_at=1)  # old boundary, not among the newest

    counts = reap_stale_checkpoints(pool)

    # newest (1) + the boundary (1) survive; the boundary's blob stays too
    assert counts == ReapCounts(agents=1, checkpoints=6, writes=6, blobs=0)
    assert _count(pool, "checkpoints", "271") == 2
    assert _count(pool, "checkpoint_blobs", "271") == 1  # shared blob referenced by both


def test_resurrect_race_skips_instead_of_reaping(
    pool: ConnectionPool[Any], monkeypatch: pytest.MonkeyPatch
) -> None:

    _agent(pool, 301, "terminated")
    _thread(pool, "301", 5)

    # The candidate scan sees the agent as terminated; a resurrect flips the
    # status before the per-agent trim. The pass re-checks eligibility right
    # before the trim — pin that guard by making it say "not stale anymore".
    monkeypatch.setattr(reaper, "_still_stale", lambda _pool, _aid: False)  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType]

    counts = reap_stale_checkpoints(pool)

    assert counts == ReapCounts(agents=0, checkpoints=0, writes=0, blobs=0)
    assert _count(pool, "checkpoints", "301") == 5  # untouched


def test_pass_is_idempotent(pool: ConnectionPool[Any]) -> None:
    _agent(pool, 401, "terminated")
    _thread(pool, "401", 3)

    first = reap_stale_checkpoints(pool)
    second = reap_stale_checkpoints(pool)

    assert first == ReapCounts(agents=1, checkpoints=2, writes=2, blobs=0)
    assert second == ReapCounts(agents=0, checkpoints=0, writes=0, blobs=0)
    assert _count(pool, "checkpoints", "401") == 1


def test_sync_trim_matches_counts(pool: ConnectionPool[Any]) -> None:
    """The sync trim helper (the reaper's building block) matches the async
    one's semantics: keep latest, cascade writes, keep referenced blobs."""
    _thread(pool, "501", 7, channel_version="shared-v1")

    counts = trim_checkpoints_sync(pool, "501", keep=1)

    assert counts.checkpoints == 6
    assert counts.writes == 6
    assert counts.blobs == 0  # the survivor references the shared blob
    assert _count(pool, "checkpoints", "501") == 1
    assert _count(pool, "checkpoint_writes", "501") == 1
    assert _count(pool, "checkpoint_blobs", "501") == 1


def test_missing_checkpoints_table_is_a_noop(pool: ConnectionPool[Any]) -> None:
    """A DB without the saver's `checkpoints` table (fresh cluster, or a
    throwaway DB carrying only events tables) reads as an empty pass — the
    daemon's full-maintenance tests run the reaper against exactly such a DB."""

    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute("DROP TABLE checkpoints")

    assert reaper._thread_counts(pool) == {}
    assert reap_stale_checkpoints(pool) == ReapCounts(agents=0, checkpoints=0, writes=0, blobs=0)


def test_sync_trim_batches_large_thread(pool: ConnectionPool[Any]) -> None:
    """A thread with more doomed checkpoints than one batch is trimmed in
    multiple short statements, counting exactly (Task #1130)."""
    _agent(pool, 202, "terminated")
    _thread(pool, "202", 120)

    counts = trim_checkpoints_sync(pool, "202", keep=1, batch=50)

    # 119 doomed -> 3 statements (50 + 50 + 19); survivor + its write + blob stay.
    assert counts == TrimCounts(checkpoints=119, writes=119, blobs=0)
    assert _count(pool, "checkpoints", "202") == 1
    assert _count(pool, "checkpoint_writes", "202") == 1
    assert _count(pool, "checkpoint_blobs") == 1  # survivor's blob survives


def test_reap_caps_agents_per_pass(pool: ConnectionPool[Any]) -> None:
    """A burst of stale candidates is spread over passes, not trimmed all at
    once; the remainder stay candidates for the next pass (idempotent)."""
    for aid in range(301, 371):  # 70 terminated agents, 4 checkpoints each
        _agent(pool, aid, "terminated")
        _thread(pool, str(aid), 4)

    counts = reap_stale_checkpoints(pool)

    assert counts.agents == 64  # _STALE_MAX_AGENTS_PER_PASS
    assert counts.checkpoints == 64 * 3
    # Second pass finishes the rest.
    counts2 = reap_stale_checkpoints(pool)
    assert counts2.agents == 6
    assert counts2.checkpoints == 6 * 3


def test_boundary_only_thread_is_not_a_stale_candidate(pool: ConnectionPool[Any]) -> None:
    """A terminated thread whose ONLY remaining checkpoints are compaction
    boundaries has nothing trim can delete — it must not be a permanent
    candidate that starves real reclamation under the per-pass cap."""
    _agent(pool, 401, "terminated")
    _thread(pool, "401", 3, boundary_at=0)  # 3 ckpts, oldest is boundary
    # trim to keep=1 (Rule B): newest + boundary survive = 2 rows, both un-deletable
    trim_checkpoints_sync(pool, "401", keep=1)
    assert _count(pool, "checkpoints", "401") == 2

    # With a second thread holding real doomed rows, the cap must reach it even
    # though 401 (id smaller) is first in the candidate list.
    _agent(pool, 402, "terminated")
    _thread(pool, "402", 5)

    counts = reap_stale_checkpoints(pool)
    assert counts.agents == 1
    assert counts.checkpoints == 4
    assert _count(pool, "checkpoints", "402") == 1


def test_starvation_eight_noop_candidates(pool: ConnectionPool[Any]) -> None:
    """8 no-op candidates (boundary+newest only) must not starve a real
    candidate under the per-pass cap — the #2226 review regression."""
    for aid in range(101, 109):
        _agent(pool, aid, "terminated")
        _thread(pool, str(aid), 3, boundary_at=0)
        trim_checkpoints_sync(pool, str(aid), keep=1)  # newest + boundary only
    _agent(pool, 200, "terminated")
    _thread(pool, "200", 5)

    counts = reap_stale_checkpoints(pool)
    assert counts.agents == 1, counts
    assert counts.checkpoints == 4, counts
    assert _count(pool, "checkpoints", "200") == 1
