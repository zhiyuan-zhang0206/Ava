"""Labeler per-agent failure backoff — the bound that turns a persistently
failing label from a ~1/s hot loop (build_chat_model + LLM round-trip every
poll) into an exponentially-spaced retry. See services/labeler/daemon.py.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator

import psycopg
import pytest

from services.labeler import daemon
from shared.config import settings
from shared.daemon_health import Liveness
from shared.db import create_agent, pool


@pytest.fixture(autouse=True)
def _clear_backoff_state() -> Iterator[None]:
    """The backoff registry is module-global (per daemon process); reset it so
    each test starts from empty and does not leak into the next."""
    daemon._BACKOFF.clear()
    yield
    daemon._BACKOFF.clear()


def test_record_failure_is_exponential_and_capped() -> None:
    now = 1000.0
    # 1st..5th failure: 2, 4, 8, 16, 32s
    expected = [2.0, 4.0, 8.0, 16.0, 32.0]
    for i, want in enumerate(expected, start=1):
        delay = daemon._record_failure(1, now)
        assert delay == want
        fails, deadline = daemon._BACKOFF[1]
        assert fails == i
        assert deadline == now + want

    # Keep failing until the cap binds — delay never exceeds _BACKOFF_CAP_S.
    delay = 0.0
    for _ in range(20):
        delay = daemon._record_failure(1, now)
    assert delay == daemon._BACKOFF_CAP_S


def test_cooling_ids_returns_only_in_window() -> None:
    now = 1000.0
    daemon._record_failure(1, now)  # deadline now+2 -> cooling at now
    daemon._record_failure(2, now)  # deadline now+2 -> cooling at now

    assert sorted(daemon._cooling_ids(now)) == [1, 2]
    # Just past agent 1+2's 2s window: neither is cooling anymore.
    assert daemon._cooling_ids(now + 3.0) == []


def test_clear_backoff_drops_state() -> None:
    now = 1000.0
    daemon._record_failure(7, now)
    assert 7 in daemon._BACKOFF
    daemon._clear_backoff(7)
    assert 7 not in daemon._BACKOFF
    # Idempotent — clearing an unknown id is a no-op, not a KeyError.
    daemon._clear_backoff(7)


def test_agent_is_retired_after_the_give_up_threshold() -> None:
    """Backoff bounds the retry RATE; the give-up bounds their NUMBER. Past the
    threshold the agent stays out of the poll SELECT for good, so a
    permanently-unlabelable agent costs a fixed number of LLM calls rather than
    ~12/hour forever (issue #178 enlarged the population that can reach this)."""
    now = 1000.0
    for _ in range(daemon._GIVE_UP_AFTER_FAILURES - 1):
        daemon._record_failure(1, now)
    assert not daemon._is_retired(1)
    # Still merely cooling: once its window passes it would be retried.
    assert daemon._cooling_ids(now + daemon._BACKOFF_CAP_S + 1.0) == []

    daemon._record_failure(1, now)
    assert daemon._is_retired(1)
    # Retired: excluded no matter how far past the deadline the clock is.
    assert daemon._cooling_ids(now + daemon._BACKOFF_CAP_S * 100) == [1]


def test_retired_entry_is_never_pruned_as_stale() -> None:
    """The stale-prune exists to drop agents labeled out of band. Applying it to
    a retired agent would readmit it to the SELECT and restart the whole
    attempt cycle — the unbounded loop the give-up exists to stop."""
    now = 1000.0
    daemon._BACKOFF[5] = (daemon._GIVE_UP_AFTER_FAILURES, now - daemon._BACKOFF_CAP_S * 10)

    assert daemon._cooling_ids(now) == [5]
    assert 5 in daemon._BACKOFF


def test_a_successful_label_clears_a_retired_agent() -> None:
    """Retirement is not a tombstone: consecutive failures are what count, so a
    label that lands (e.g. after a daemon restart, or a user PATCH racing in)
    drops the state like any other success."""
    now = 1000.0
    for _ in range(daemon._GIVE_UP_AFTER_FAILURES):
        daemon._record_failure(9, now)
    assert daemon._is_retired(9)

    daemon._clear_backoff(9)
    assert not daemon._is_retired(9)
    assert daemon._cooling_ids(now) == []


def test_retry_note_stops_promising_a_retry_once_retired() -> None:
    """The failure log line must not name a next retry that will never come."""
    now = 1000.0
    delay = daemon._record_failure(3, now)
    assert "next retry" in daemon._retry_note(3, delay)

    for _ in range(daemon._GIVE_UP_AFTER_FAILURES - 1):
        delay = daemon._record_failure(3, now)
    assert "retired" in daemon._retry_note(3, delay)
    assert "next retry" not in daemon._retry_note(3, delay)


def test_cooling_ids_prunes_long_stale_entries() -> None:
    now = 1000.0
    daemon._record_failure(1, now)  # deadline now+2 (recent, will linger as expired-but-fresh)
    # A stale entry whose retry was due more than a full cap-window ago: the
    # agent must have been labeled out of band (else it would have been
    # re-selected and cleared), so _cooling_ids drops it.
    daemon._BACKOFF[99] = (3, now - daemon._BACKOFF_CAP_S - 1.0)

    cooling = daemon._cooling_ids(now)
    assert cooling == [1]  # 1 still in window
    assert 99 not in daemon._BACKOFF  # 99 pruned
    assert 1 in daemon._BACKOFF  # 1 kept (expired by <1 cap window, still eligible)


def _seed_chat(db: psycopg.Connection, tid: int) -> None:
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO inbound_messages (agent_id, content, kind, source) "
            "VALUES (%s, %s, 'chat', 'user')",
            (tid, "do the thing"),
        )


@pytest.mark.asyncio
async def test_dispatch_loop_uses_labeler_model_not_main_model(
    db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The poll loop labels with settings.lm.labeler_model (its own knob), not
    settings.lm.llm_model (the main reasoning model). Pin the two to different
    values and assert the model handed to generate_label_async is the labeler
    one — guards against a future edit reverting the daemon to llm_model."""
    monkeypatch.setattr(settings.lm, "llm_model", "deepseek-v4-pro")
    monkeypatch.setattr(settings.lm, "labeler_model", "deepseek-v4-flash")
    monkeypatch.setattr(daemon, "_POLL_INTERVAL_S", 0.0)

    a = create_agent(db_conn)
    _seed_chat(db_conn, a)
    db_conn.commit()

    captured: list[str] = []

    async def _capture(_tid: int, _prompt: str, model: str) -> None:
        captured.append(model)
        # Break the otherwise-infinite poll loop after the first dispatch.
        raise asyncio.CancelledError

    monkeypatch.setattr(daemon, "generate_label_async", _capture)

    p = pool()
    try:
        with pytest.raises(asyncio.CancelledError):
            await daemon._dispatch_loop(p, Liveness(daemon._LIVENESS_TIMEOUT_S))
    finally:
        p.close()

    assert captured == ["deepseek-v4-flash"]


def test_select_unlabeled_excludes_cooling_ids(db_conn: psycopg.Connection) -> None:
    """The SQL-level exclusion (`AND NOT (t.id = ANY(%s::bigint[]))`) works for
    both an empty cooling list and a non-empty one — empty selects all, a
    populated list drops exactly those ids from the LIMIT window."""
    a = create_agent(db_conn)
    b = create_agent(db_conn)
    _seed_chat(db_conn, a)
    _seed_chat(db_conn, b)

    with db_conn.cursor() as cur:
        # Empty cooling -> the array param is `'{}'::bigint[]`, excludes nothing.
        all_ids = {tid for tid, _prompt in daemon._select_unlabeled(cur, [])}
        assert {a, b} <= all_ids

        # a is cooling -> excluded; b still selectable.
        remaining = {tid for tid, _prompt in daemon._select_unlabeled(cur, [a])}
        assert a not in remaining
        assert b in remaining


@pytest.mark.asyncio
async def test_dispatch_loop_backs_off_on_llm_failure(
    db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression (audit round 2, P1): generate_label_async swallows LLM
    failures (returns False), so the daemon's old except-keyed backoff was
    dead code — a bad API key hot-looped a full LLM call every second. The
    backoff must fire on the RETURN value: one LLM attempt, then the agent
    is cooling and no further calls happen."""
    monkeypatch.setattr(daemon, "_POLL_INTERVAL_S", 0.0)

    a = create_agent(db_conn)
    _seed_chat(db_conn, a)
    db_conn.commit()

    from services.labeler import labeler

    llm_calls = {"n": 0}

    def _boom(model: str, **kwargs: object) -> None:
        llm_calls["n"] += 1
        raise RuntimeError("bad API key")

    monkeypatch.setattr(labeler, "build_chat_model", _boom)

    p = pool()
    task = asyncio.create_task(daemon._dispatch_loop(p, Liveness(daemon._LIVENESS_TIMEOUT_S)))
    try:
        await asyncio.sleep(0.4)  # several poll rounds
        assert llm_calls["n"] == 1, "a failing label must back off, not hot-loop"
        assert a in daemon._BACKOFF, "the agent entered failure backoff"
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        p.close()
