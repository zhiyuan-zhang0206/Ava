"""Unit tests for the loguru -> event-emitter adapter — locks event resolution /
agent_id sentinel / payload shape on the JSONL mirror (the durable local copy
of the unified event stream).

The loguru handler enqueues into the unified emitter (`shared.telemetry`),
whose drain thread batch-writes; `_last_event` flushes the queue first so
assertions see the written lines without sleeps. The Postgres `events` copy
was retired with the LGTM cutover (task #1197 close-C): the PG table is a
read-only archive, so these assertions read the day-stamped JSONL mirror
(`logs_dir()/events-YYYYMMDD.jsonl`) instead — same row shape, one JSON
object per line.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import psycopg
import pytest
from loguru import logger as _global_logger

from shared import telemetry
from shared.log import _add_postgres_sink, _postgres_sink


@pytest.fixture
def sink_logger():
    """Register the loguru -> emitter adapter + bind agent_id, cleanup after yield.

    The adapter enqueues (non-blocking); tests flush via `_last_event`. bind
    agent_id="-" simulates the gateway init form; within tests when the agent
    process perspective is needed, logger.bind() overrides it.
    """
    _add_postgres_sink()  # eager open pipeline (pool + drain thread)
    _global_logger.remove()
    sink_id = _global_logger.add(_postgres_sink, level="INFO", enqueue=False, catch=False)
    _global_logger.configure(extra={"agent_id": "-"})
    yield _global_logger
    _global_logger.remove(sink_id)


@pytest.fixture(autouse=True)
def _isolate_events_mirror(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Each test's event mirror lives in its own tmp dir, never the
    worker-shared session home (audit M-2): the drain thread appends to
    `logs_dir()` by day, so a later test in the same worker would otherwise
    read earlier tests' lines (order-dependent failures or false passes).

    `paths.ava_home` is the patch target rather than `paths.logs_dir` because
    the telemetry module bound `logs_dir` at import time — patching the path
    function itself would make the drain write to the session home while
    `_last_event` read the tmp dir. Both sides resolve `ava_home()` at call
    time, so one patch redirects the whole pipeline consistently."""
    from shared import paths

    monkeypatch.setattr(paths, "ava_home", lambda: tmp_path / "ava_home")


def _last_event() -> tuple[str, int | None, str, dict]:
    """Flush the emitter, then return the last mirror line (event_name,
    agent_id, level, attributes). Fail loud on an empty mirror."""
    from shared.paths import logs_dir

    telemetry.sync()
    day = datetime.now(UTC).strftime("%Y%m%d")
    path = logs_dir() / f"events-{day}.jsonl"
    if not path.exists():
        raise AssertionError("events mirror is empty — test didn't emit any event")
    lines = [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert lines, "events mirror is empty — test didn't emit any event"
    obj = json.loads(lines[-1])
    return obj["event_name"], obj.get("agent_id"), obj.get("level"), obj.get("attributes", {})


def test_sink_event_resolution_explicit_event(sink_logger) -> None:
    """Explicit event= has priority, not overridden by label= fallback."""
    sink_logger.info("foo", event="turn_end", label="ignored")  # pyright: ignore[reportUnknownMemberType]
    event, _agent_id, _level, payload = _last_event()
    assert event == "turn_end"
    assert payload["label"] == "ignored"  # label still in payload, not lost


def test_sink_event_resolution_label_fallback(sink_logger) -> None:
    """When no event=, falls back to label= (compatible with existing [exec]/[claim] pattern)."""
    sink_logger.info("[{label}] {body}", label="exec", body="output...")  # pyright: ignore[reportUnknownMemberType]
    event, _agent_id, _level, _payload = _last_event()
    assert event == "exec"


def test_sink_event_resolution_log_default(sink_logger) -> None:
    """Bare logger.info with no event/label → defaults to "log"."""
    sink_logger.info("bare message")  # pyright: ignore[reportUnknownMemberType]
    event, _agent_id, _level, _payload = _last_event()
    assert event == "log"


def test_sink_empty_event_raises(sink_logger) -> None:
    """Explicit event="" must raise (caller bug, should not silently fall through).
    catch=False lets ValueError propagate out; in real deployment with catch=True,
    loguru would write the stack to stderr, but this test cannot assert stderr behavior."""
    with pytest.raises(ValueError, match="empty event"):
        sink_logger.info("foo", event="")  # pyright: ignore[reportUnknownMemberType]


def test_sink_agent_id_dash_sentinel_maps_to_null(sink_logger) -> None:
    """agent_id="-" (gateway init default) → DB NULL."""
    sink_logger.info("from gateway", event="sse_drop")  # pyright: ignore[reportUnknownMemberType]
    _event, agent_id, _level, _payload = _last_event()
    assert agent_id is None


def _insert_agent(db: psycopg.Connection) -> int:
    """The mirror keeps the agent dimension; use a real agents row (the row the events would have referenced) so the assertion is against a real id."""
    with db.cursor() as cur:
        cur.execute("INSERT INTO agents (label) VALUES ('sink-test') RETURNING id")
        row = cur.fetchone()
    assert row is not None, "INSERT ... RETURNING must return a row"
    return row[0]


def test_sink_agent_id_numeric_string_converts_to_int(
    sink_logger,
    db_conn: psycopg.Connection,
) -> None:
    """agent_id="42" (init_agent_process bind) → the mirror row carries 42."""
    tid = _insert_agent(db_conn)
    db_conn.commit()
    sink_logger.bind(agent_id=str(tid)).info("from agent", event="sse_drop")  # pyright: ignore[reportUnknownMemberType]
    _event, agent_id, _level, _payload = _last_event()
    assert agent_id == tid


def test_stdlib_intercept_routes_through_sink(sink_logger) -> None:
    """stdlib `logging.getLogger(...).info(...)` goes through _StdlibInterceptHandler →
    loguru sink → agent_events INSERT. Verifies that service modules (e.g. `services/agent_ops/daemon.py`
    that use stdlib logging) have their logs go to DB after upgrade, without needing to rewrite callsites line by line."""
    import logging

    from shared.log import _install_stdlib_intercept

    _install_stdlib_intercept()
    stdlib_log = logging.getLogger("test.stdlib.intercept")
    stdlib_log.warning("stdlib warn via intercept handler")
    event, _agent_id, level, payload = _last_event()
    assert level == "warning"
    # event defaults to "log" (no explicit event=), message goes into payload.msg
    assert event == "log"
    assert payload["msg"] == "stdlib warn via intercept handler"


def test_sink_agent_id_int_kwarg_overrides_bind(sink_logger, db_conn: psycopg.Connection) -> None:
    """log call passing agent_id=N overrides the bind default — used by shared/agents.py for cross-process
    lifecycle events (the caller process bind may not be the target)."""
    tid = _insert_agent(db_conn)
    db_conn.commit()
    sink_logger.info("spawned", event="agent_spawned", agent_id=tid)  # pyright: ignore[reportUnknownMemberType]
    _event, agent_id, _level, _payload = _last_event()
    assert agent_id == tid


def test_sink_payload_excludes_meta_columns_includes_msg(
    sink_logger,
) -> None:
    """payload jsonb does not duplicate agent_id/event (columns already store them); msg goes into payload
    as a debug grep entry point."""
    sink_logger.info("hello {name}", event="sse_drop", name="world", custom_field=1)  # pyright: ignore[reportUnknownMemberType]
    _event, _agent_id, _level, payload = _last_event()
    assert "agent_id" not in payload
    assert "agent_id" not in payload
    assert "event" not in payload
    assert payload["msg"] == "hello world"  # loguru formatted
    assert payload["name"] == "world"
    assert payload["custom_field"] == 1


def test_sink_preserves_llm_usage_source_in_payload(sink_logger) -> None:
    """A usage-path discriminator must not be consumed as event provenance."""
    sink_logger.info(  # pyright: ignore[reportUnknownMemberType]
        "metered web answer",
        event="llm_usage",
        source="web.fetch",
        transport_source="system",
        calls=1,
    )

    _event, _agent_id, _level, payload = _last_event()
    assert payload["source"] == "web.fetch"
    assert "transport_source" not in payload


def test_sink_level_is_recorded(sink_logger) -> None:
    """WARNING / ERROR level goes into DB level column — used by sidebar warn_24h/err_24h."""
    sink_logger.warning("uh oh", event="sse_drop")  # pyright: ignore[reportUnknownMemberType]
    _event, _agent_id, level, _payload = _last_event()
    assert level == "warning"

    sink_logger.error("boom", event="sse_drop")  # pyright: ignore[reportUnknownMemberType]
    _event, _agent_id, level, _payload = _last_event()
    assert level == "error"


# ─── exception → payload (traceback / type / value) ───
#
# 161 incident: turn_end ok=False but events.payload had no traceback — logger.opt
# (exception=True) left a stack in stderr / file sink, but _postgres_sink only dumped
# extra, record["exception"] was not consumed, so the DB couldn't diagnose whether the LLM
# had a timeout or a decode error. Fix: _postgres_sink detects record["exception"] and automatically
# injects traceback / exception_type / exception_value into payload.


def test_sink_exception_includes_traceback_in_payload(
    sink_logger,
) -> None:
    """When logger.opt(exception=True), payload should carry traceback / exception_type /
    exception_value fields — for diagnosing exception turns in the events table (turn_end ok=False
    / process_exit reason='exception:X' scenarios)."""
    try:
        raise RuntimeError("simulated LLM timeout")  # noqa: TRY301 — set sys.exc_info()
    except RuntimeError:
        sink_logger.opt(exception=True).warning("turn ended", event="turn_end", ok=False)  # pyright: ignore[reportUnknownMemberType]

    _event, _agent_id, _level, payload = _last_event()
    assert payload["exception_type"] == "RuntimeError"
    assert payload["exception_value"] == "simulated LLM timeout"
    assert "Traceback" in payload["traceback"]
    assert "RuntimeError: simulated LLM timeout" in payload["traceback"]


def test_sink_no_exception_no_extra_fields(sink_logger) -> None:
    """Normal logger.info should not have traceback / exception_* fields — avoids payload bloat,
    and lets consumers (sidebar / SQL) use `payload ? 'traceback'` to detect exception turns."""
    sink_logger.info("normal turn", event="turn_end", ok=True)  # pyright: ignore[reportUnknownMemberType]
    _event, _agent_id, _level, payload = _last_event()
    assert "traceback" not in payload
    assert "exception_type" not in payload
    assert "exception_value" not in payload


def test_sink_opt_exception_without_active_exc_skips_garbage_payload(
    sink_logger,
) -> None:
    """`logger.opt(exception=True)` when sys.exc_info() == (None,None,None) still
    sets record["exception"] as a namedtuple with all three fields None (not Python None).
    Previously the sink would format that directly, producing `traceback="NoneType: None\\n"` + exception_value=
    "None" garbage payload (observed in 167/168 events).

    Fix: `exc.type is not None` guard skips empty records. This test locks the guard behavior —
    even if a caller mistakenly uses opt(exception=True), no garbage fields will pollute the payload."""
    # outside an except block, opt(exception=True) → no active exception
    sink_logger.opt(exception=True).warning("no active exc", event="sse_drop")  # pyright: ignore[reportUnknownMemberType]

    _event, _agent_id, _level, payload = _last_event()
    assert "traceback" not in payload, (
        f"empty exception should not inject traceback; got {payload.get('traceback')!r}"  # pyright: ignore[reportUnknownMemberType]
    )
    assert "exception_type" not in payload
    assert "exception_value" not in payload


# ─── rollout-window quieting (task #731) ────────────────────────────────────
#
# User ruling 2026-08-04: while a cluster deploy holds the update lease, the
# rollout's predictable side effects (ops manager rounds blocked by
# pause/schema/pin, slow pool acquires, DB-outage pauses, query cancellations)
# must not alarm at WARNING in the events table. The downgrade lives in
# `_message_to_params` (the one place both sink paths derive rows), gated on
# `_deploy_in_progress()` — monkeypatched here to keep the tests hermetic (no
# real lease reads against the test DB).


@pytest.fixture(autouse=True)
def _reset_deploy_cache():
    """Each test starts with a cold deploy cache — the module-level cache is a
    process singleton and would otherwise leak a True/False across tests."""
    import shared.log as slog

    slog._deploy_cached = cast(
        bool | None, None
    )  # keep bool|None for pyright (bare None literal narrows every later read)
    slog._deploy_cached_at = 0.0
    yield


def test_rollout_quiet_db_pool_acquire_slow(
    monkeypatch: pytest.MonkeyPatch,
    sink_logger,
) -> None:
    """db_pool_acquire_slow during a deploy → INFO in the events row (file sink
    keeps the original level; only the PG row is quieted)."""
    import shared.log as slog

    monkeypatch.setattr(slog, "_deploy_in_progress", lambda: True)
    sink_logger.warning(  # pyright: ignore[reportUnknownMemberType]
        "[db pool] acquire took 2.0s (slow — Postgres under load)",
        event="db_pool_acquire_slow",
        name="ops",
        elapsed=2.0,
    )
    _event, _agent_id, level, _payload = _last_event()
    assert level == "info"


def test_rollout_quiet_db_outage_events(
    monkeypatch: pytest.MonkeyPatch,
    sink_logger,
) -> None:
    """Every db_outage_* category is quieted while a deploy holds the lease."""
    import shared.log as slog

    monkeypatch.setattr(slog, "_deploy_in_progress", lambda: True)
    for event in ("db_outage_wait", "db_outage_pause", "db_outage_reconcile_retry"):
        sink_logger.warning("db unreachable — pausing", event=event, agent_id="-")  # pyright: ignore[reportUnknownMemberType]
        _event, _agent_id, level, _payload = _last_event()
        assert level == "info", f"{event} should be quieted to INFO"


def test_rollout_quiet_ops_manager_round_blocked(
    monkeypatch: pytest.MonkeyPatch,
    sink_logger,
) -> None:
    """The ops manager's stdlib-logged 'round blocked' line (event='log', no
    event=) is quieted by message prefix while a deploy holds the lease."""
    import shared.log as slog

    monkeypatch.setattr(slog, "_deploy_in_progress", lambda: True)
    sink_logger.warning(  # pyright: ignore[reportUnknownMemberType]
        "[ops.manager] round blocked by pause (scope=all), roster NOT fully "
        "reconciled — 12 consecutive round(s)"
    )
    _event, agent_id, level, _payload = _last_event()
    assert level == "info"
    assert agent_id is None  # still a gateway/ops line, not an agent event

    # ERROR escalation (>10 consecutive rounds) is quieted the same way.
    sink_logger.error(  # pyright: ignore[reportUnknownMemberType]
        "[ops.manager] round blocked by schema (scope=all), roster NOT fully "
        "reconciled — 15 consecutive round(s)"
    )
    _event, _agent_id, level, _payload = _last_event()
    assert level == "info"


def test_rollout_quiet_query_cancellation(
    monkeypatch: pytest.MonkeyPatch,
    sink_logger,
) -> None:
    """The 'query cancellation failed' line (bare warning, event='log') is
    quieted by message prefix while a deploy holds the lease."""
    import shared.log as slog

    monkeypatch.setattr(slog, "_deploy_in_progress", lambda: True)
    sink_logger.warning("query cancellation failed: cancellation timeout expired")  # pyright: ignore[reportUnknownMemberType]
    _event, _agent_id, level, _payload = _last_event()
    assert level == "info"


def test_rollout_quiet_no_deploy_keeps_warning(
    monkeypatch: pytest.MonkeyPatch,
    sink_logger,
) -> None:
    """No deploy in flight → the same categories keep their original WARNING:
    outside a rollout window these are real signals."""
    import shared.log as slog

    monkeypatch.setattr(slog, "_deploy_in_progress", lambda: False)
    sink_logger.warning("slow acquire", event="db_pool_acquire_slow", elapsed=2.0)  # pyright: ignore[reportUnknownMemberType]
    _event, _agent_id, level, _payload = _last_event()
    assert level == "warning"


def test_rollout_quiet_unrelated_warning_stays(
    monkeypatch: pytest.MonkeyPatch,
    sink_logger,
) -> None:
    """A WARNING outside the quiet categories is untouched even mid-deploy —
    the suppression list is deliberately narrow (no blanket WARNING
    suppression)."""
    import shared.log as slog

    monkeypatch.setattr(slog, "_deploy_in_progress", lambda: True)
    sink_logger.warning("real trouble", event="db_pool_acquire_timeout")  # pyright: ignore[reportUnknownMemberType]
    _event, _agent_id, level, _payload = _last_event()
    assert level == "warning"


def test_deploy_quieting_read_never_blocks_producer(
    monkeypatch: pytest.MonkeyPatch,
    sink_logger,
) -> None:
    """The quieting lease read must never block the log producer (P1, audit
    2026-08-08): the check runs in the synchronous sink on the caller's
    thread, and a lease read dials the DB — during a DB outage (exactly when
    db_outage_* warnings fire) a synchronous read froze the agent runloop for
    seconds per record. The read happens on a background thread; the producer
    gets the stale snapshot immediately."""
    import threading
    import time

    import shared.log as slog

    entered = threading.Event()
    release = threading.Event()

    def slow_read() -> bool:
        entered.set()
        release.wait(5)
        return True  # a deploy holds the lease

    monkeypatch.setattr(slog, "_read_deploy_lease", slow_read)
    slog._deploy_cached = cast(
        bool | None, None
    )  # keep bool|None for pyright (bare None literal narrows every later read)
    slog._deploy_cached_at = 0.0

    start = time.monotonic()
    sink_logger.warning("db unreachable — pausing", event="db_outage_wait", agent_id="-")  # pyright: ignore[reportUnknownMemberType]
    elapsed = time.monotonic() - start
    assert elapsed < 0.5, (
        f"producer was blocked by the quieting lease read ({elapsed:.2f}s) — "
        "the outage amplifier is back"
    )
    # The refresh is in flight on a background thread; releasing it updates
    # the snapshot asynchronously.
    assert entered.wait(1.0), "refresh thread never started"
    release.set()
    deadline = time.monotonic() + 5
    cached: bool | None = slog._deploy_cached
    while cached is not True and time.monotonic() < deadline:
        time.sleep(0.02)
        cached = slog._deploy_cached  # re-read each pass; the background thread refreshes it
    assert cached is True, "cache was not refreshed from the background read"


def test_stdlib_intercept_emit_never_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """`_StdlibInterceptHandler.emit` must never propagate (audit 2026-08-08 +
    #1862): stdlib `Handler.handle` has no try/except around emit, so a
    formatting TypeError inside a log call (im_bridge's `%d`-with-str-args
    SSE reconnect crash) or a shallow call stack (`sys._getframe(6)`
    ValueError) would otherwise kill the logger's caller."""
    import logging

    from shared.log import _StdlibInterceptHandler

    handler = _StdlibInterceptHandler()
    seen: list[logging.LogRecord] = []
    handler.handleError = seen.append  # type: ignore[method-assign]

    # getMessage raises TypeError (%d with a str argument).
    bad = logging.LogRecord("x", logging.WARNING, "path.py", 1, "%d", ("str",), None)
    handler.emit(bad)
    assert seen == [bad], "handleError not called for a formatting-failure record"

    # Shallow call stack: sys._getframe(6) raises ValueError (handled).
    seen.clear()
    good = logging.LogRecord("x", logging.WARNING, "path.py", 1, "fine", (), None)
    handler.emit(good)
    assert seen == [], "a healthy record must not hit handleError"


def test_deploy_quieting_cache_holds_answer_through_read_failure(
    monkeypatch: pytest.MonkeyPatch,
    sink_logger,
) -> None:
    """A failed refresh keeps the previous answer for the TTL: a mid-rollout DB
    blip (the gateway restart drops the data plane the lease lives in) must
    not cancel the quieting a rollout relies on."""
    import time

    import shared.log as slog

    monkeypatch.setattr(slog, "_read_deploy_lease", lambda: True)
    slog._deploy_cached = True
    slog._deploy_cached_at = time.monotonic()

    sink_logger.warning("slow acquire", event="db_pool_acquire_slow", elapsed=2.0)  # pyright: ignore[reportUnknownMemberType]
    _event, _agent_id, level, _payload = _last_event()
    assert level == "info", "cached lease must quiet within the TTL"
