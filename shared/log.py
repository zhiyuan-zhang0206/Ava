"""Structured logger — shared by all processes (kernel / gateway / SDK subprocess).

# Boundaries of the three channels (important)

| Signal | Use | Who sees it |
|---|---|---|
| Framework observability | `logger.{info,warning,error}` | log file (after step 2 lands). Subprocesses do **not** add a stderr handler, to avoid polluting exec_output. |
| SDK -> agent return value | `return` or `raise` | LLM sees the next turn (return) or traceback (raise). |
| agent -> user dialogue | AIMessage text content (callback streams chat_delta + into LangGraph state.messages) | Browser |

**SDK functions do not print side effects** — results via return
value, exceptions via raise. If the agent wants to see the return
value, it `print(result)`. SDK printing for the agent is noise:
spawning 10 prints 10 lines of hint, breaking the agent's plain
`if all_ok: ...` branch control flow.

`print()` has only two legitimate uses in the ava SDK:
1. agent actively calling user-facing dump functions like
   `ava.help(...)` (the print is the function's semantics)
2. agent's own code debug/print result (not the SDK's territory)

# Design

loguru single logger instance + `extra` dict for contextual fields.
Each entry-point process calls init_* once at startup, binding
process-level fields (agent process binds agent_id — deferred, so a
process hosting several agents' turns attributes each record to the
turn that wrote it; gateway does not; SDK subprocess binds the same
group as its parent agent). All
subsequent `from shared.log import logger` calls get a logger that
auto-carries those fields — callers do not repeat them per line.

# Field conventions

Every log line carries at least:
- `level`  (loguru built-in)
- `time`   (loguru built-in)
- `agent_id`  the turn's agent if one is bound, else this process's
  (`TurnScopedAgentId`); if neither, `-`

More granular fields (`turn_id` / `node` / `tool_name`) are added
as-needed later; not strictly required on every line.

# Output

Three sink kinds, mixed per process type by the init_* entry points:
stderr (human-readable, aligned with terminal scrollback habits), a JSONL
file sink under `logs_dir()` (rotated — see `_add_file_sink`), and the
unified event pipeline: every INFO+ record is derived to an event and
enqueued into `shared.telemetry` — the unified emitter — which batches
it into the `events` table (the unified emitter; the legacy
`agent_events` mirror was removed with the migration window — see
`shared/telemetry.py`).

Subprocesses call `init_subprocess_logger` — file sink **only**, no
stderr: a subprocess's stderr is captured by the parent and injected
into the exec_output fed to the LLM, so framework logs on stderr would
pollute the agent context.
"""

from __future__ import annotations

import contextlib
import datetime
import logging
import os
import sys
import threading
import time
import traceback as _traceback
from pathlib import Path
from typing import Any, cast

import loguru
from loguru import logger

from shared import process_sha
from shared.turn_identity import (
    TURN_SCOPED_AGENT_ID,
    TurnScopedAgentId,
    set_process_agent_id,
)

# `shared.machine` / `shared.paths` are imported inside the init functions that
# use them, never at module top: both pull the pydantic Settings chain (+~30 MB
# RSS per process, measured 2026-08-13), and `import shared.log` must stay
# cheap for lightweight processes that hold a logger but never call an init_*
# (the per-session pty hosts — one process per live shell — are the forcing
# case). An init_* caller is a real service and pays settings anyway.

__all__ = [
    "init_agent_process",
    "init_cli_process",
    "init_gateway_process",
    "init_subprocess_logger",
    "logger",
]


def _machine_name_lazy() -> str:
    """`shared.machine.machine_name`, imported at call time (the module pulls
    the Settings chain — see the import note at the top of this file)."""
    from shared.machine import machine_name

    return machine_name()


class _StdlibInterceptHandler(logging.Handler):
    """Route stdlib logging through loguru so every `_log = logging.getLogger(...)`
    callsite in the codebase lands in the loguru sinks (stderr / file / PG)
    without per-file rewrites.

    Many services (`services/agent_ops/daemon.py`, `gateway/app.py`, etc.) use
    stdlib `logging.getLogger(__name__)` for historical reasons. Without
    this handler their INFO/WARNING/ERROR lines reach stderr only and never
    hit the unified `events` table that loguru's `_postgres_sink` writes
    to. The handler is installed by every `init_*` entry point below; the
    intercepted records carry whatever `extra` (e.g. agent_id) the calling
    process has bound on the loguru singleton via `logger.configure(...)`.

    Reference: this is the standard pattern documented in loguru's README
    ("Entirely compatible with standard logging").
    """

    def emit(self, record: logging.LogRecord) -> None:
        # Never raise out of emit: stdlib `logging.Handler.handle` has no
        # try/except around the call, so an exception here propagates into
        # the logger's caller and can kill it. That is not hypothetical —
        # im_bridge's SSE reconnect path died exactly this way (#1862): a
        # log call with a `%d` placeholder fed a str argument raised
        # TypeError inside emit, and the subscribing coroutine went down
        # with it. `handleError` is the stdlib's own convention for a
        # failing handler (prints the traceback to stderr).
        try:
            try:
                level = logger.level(record.levelname).name
            except ValueError:
                level = record.levelno

            # Walk the call stack so the loguru record reports the original
            # caller's filename + lineno, not this shim's. We start at depth 6
            # (the conventional offset for stdlib logging -> handler.emit
            # invocation) and skip past any further frames still inside the
            # stdlib `logging` module. Depth 6 is a convention, not a
            # guarantee — a non-standard call chain can sit shallower, and
            # sys._getframe then raises ValueError (handled: the record is
            # logged without the stack walk).
            try:
                frame: Any = sys._getframe(6)
            except ValueError:
                frame = None
            depth = 6
            while frame and frame.f_code.co_filename == logging.__file__:
                frame = frame.f_back
                depth += 1

            logger.opt(depth=depth, exception=record.exc_info).log(level, record.getMessage())
        except Exception:
            self.handleError(record)


# Top-level logger names for our own code (plus `ci-autoscale`, a bare
# string name rather than a dotted module path — see ops/ci_autoscale/*).
# `_install_stdlib_intercept` raises these back to DEBUG so a controller's
# `_log.debug(...)` (e.g. `ops.controllers.respawn`'s gateway-health
# deferral line, #1126) reaches the file sink instead of being dropped at
# the root INFO floor. Every dotted child (`ops.controllers.respawn` under
# `ops`) inherits the DEBUG level via stdlib's own ancestor lookup
# (`Logger.getEffectiveLevel`) — nothing to register per-module. Third-party
# libraries (httpx / psycopg / urllib3 / langchain / uvicorn / ...) are
# deliberately absent, so their un-audited DEBUG volume stays behind the
# root floor; see `_install_stdlib_intercept`.
_FIRST_PARTY_LOGGER_NAMES = (
    "ops",
    "services",
    "shared",
    "gateway",
    "agent",
    "ava_builtins",
    "ci-autoscale",
)


def _install_stdlib_intercept() -> None:
    """Install the intercept handler on the root logger. Idempotent — the
    `init_*` functions can call it on every startup; we replace the
    handler list rather than appending, so reinit doesn't accumulate.

    Level INFO is the floor for everyone — it keeps third-party DEBUG spam
    (httpx / psycopg / urllib3) out without an extra filter, matching
    `_add_postgres_sink`'s own threshold. `_FIRST_PARTY_LOGGER_NAMES` then
    raises that floor back to DEBUG for our own code only, so a controller's
    or daemon's `_log.debug(...)` still reaches the file sink (which is
    itself level=DEBUG — see `_add_file_sink`) the way a native
    `from shared.log import logger; logger.debug(...)` call always has.
    Before this, EVERY stdlib DEBUG record was dropped at the root before
    reaching loguru at all — #1126: 68 days of `restarter.log` had zero
    occurrences of a line that fires every time the respawn controller's
    gateway-health gate defers a restart.
    """
    logging.basicConfig(handlers=[_StdlibInterceptHandler()], level=logging.INFO, force=True)

    for _name in _FIRST_PARTY_LOGGER_NAMES:
        logging.getLogger(_name).setLevel(logging.DEBUG)

    # psycopg_pool logs benign connection-recycling churn ("discarding closed
    # connection" / "closing returned connection") at WARNING on every pool
    # maintenance pass — pure noise that floods the events table. Gate the pool
    # logger to ERROR so only real pool faults survive.
    logging.getLogger("psycopg.pool").setLevel(logging.ERROR)

    # uvicorn.access logs one INFO line per HTTP request — with the gateway's
    # uvicorn.run(log_config=None) those would flood the file sink and the
    # events table (a busy gateway emits thousands/day). Gate to WARNING:
    # only abnormal access records survive. uvicorn.error is deliberately
    # left at INFO (inherits the root floor) — that logger carries startup
    # lines AND the unhandled-ASGI-exception tracebacks #970 exists to
    # capture ("Exception in ASGI application ..." with the full stack).
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)


# Log files for all processes live under $AVA_HOME/logs (logs_dir()). kernel /
# subprocess / gateway each write their own file (kernel and subprocess share a
# single agent-{N}.log; multi-process append relies on O_APPEND atomicity;
# single-line JSONL < PIPE_BUF 4KB does not interleave).

# Remove the default stderr handler that loguru adds automatically —
# the init functions below add their own per process type, avoiding
# double handlers / duplicate prints.
logger.remove()

# extra defaults. format string references {extra[...]}; missing keys
# raise KeyError, so pre-fill.
#
# The deferred binding rather than a bare "-": a process that never calls
# `init_agent_process` still resolves to "-" (no turn bound, no process agent),
# so gateway / daemon / CLI attribution is unchanged — but a process that binds
# a TURN gets that turn's agent. That is the hosted agent-runner
# (`future/infra/agent-runner-as-server.md`), which inits through
# `init_gateway_process` and would otherwise stamp every hosted agent's records
# with the `-` sentinel, throwing away the attribution the turn contextvar knows.
logger.configure(extra={"agent_id": TURN_SCOPED_AGENT_ID})

# Human-readable format — agent_id helps distinguish when
# multiple agents run concurrently. `{extra[agent_id]:>3}` is
# right-aligned 3 chars — IDs up to 3 digits stay column-aligned,
# 4+ digits expand automatically.
_HUMAN_FORMAT = (
    "<green>{time:HH:mm:ss.SSS}</green> "
    "<level>{level: <5}</level> "
    "<cyan>a={extra[agent_id]:>3}</cyan> "
    "{message}"
)


_FILE_SINK_SIZE_LIMIT = 100 * 1024 * 1024


def _rotate_by_size_or_day(message: Any, file: Any) -> bool:
    """Rotation predicate for the JSONL file sink (Task #434): rotate when
    the file crosses the 100 MB ceiling OR when the message's date moves past
    the file's creation date.

    A pure size rotation lets a long-lived active file hold weeks of history
    (a 6+ week `agent-{N}.log` made a 6/10-11 PgListener flood read as a
    "last-24h" warning, task #426). A pure time rotation would let a chatty
    file balloon past the old ceiling. This keeps both bounds.

    The time half is anchored on the FILE's own ctime (via `file.name`, the
    current base path), not on per-process state — so the kernel and exec
    processes sharing `agent-{N}.log` evaluate the SAME decision instead of
    drifting apart. The rename race itself (one process writing while the
    other rotates) is unchanged from the pure-size design and stays accepted:
    the window is a single write and the lines land in the rotated file.

    The day boundary is UTC midnight, not the host's local midnight (tz
    audit, 2026-08, PR-6): the record's own tzinfo used to decide this
    (`message.record["time"].tzinfo`, which loguru stamps from the machine's
    local zone) made the split point drift per host — a Beijing gateway and
    a Pacific agent-runner rotated their files ~15-16 hours apart for what is
    otherwise the same calendar day everywhere else in this codebase. This
    shifts existing clusters' logfile split points to UTC midnight — ops-
    visible, but only at the moment a file happens to rotate.
    """
    with contextlib.suppress(OSError, ValueError):
        file.seek(0, 2)
        if file.tell() > _FILE_SINK_SIZE_LIMIT:
            return True
    try:
        created = datetime.datetime.fromtimestamp(
            Path(file.name).stat().st_ctime, tz=datetime.UTC
        ).date()
    except (OSError, ValueError, TypeError):
        return False
    return message.record["time"].astimezone(datetime.UTC).date() > created


def _add_file_sink(path: Path) -> int:
    """Add a JSONL file sink (`serialize=True` makes loguru serialize
    the entire record into a single JSON line — for jq / programmatic
    consumption), rotating at 100 MB or once per day and keeping 7 days.

    Unbounded, these files only grow: a long-lived `gateway.log` reached
    ~900 MB with nothing to reclaim it, and a busy `agent-{N}.log` ~190 MB.

    `enqueue=False` (loguru's default) is deliberate. loguru's documented
    answer for a sink shared by several processes is `enqueue=True`, but that
    queue allocates a POSIX semaphore which a force-killed process leaks
    permanently — and agents are SIGKILLed routinely. Enough leaks exhaust
    `kern.posix.sem.max`, after which every new agent dies at startup with
    errno 28.

    `agent-{N}.log` is the one file two processes share (kernel + exec
    subprocess — see `init_subprocess_logger`). Each holds its own handler
    and evaluates rotation independently, so a rotation racing the other
    process's write can land its next lines in the just-rotated file
    instead of the fresh one. Accepted rather than fixed: the window is a
    single write, the lines are still on disk in the rotated file — whereas
    no rotation fills the disk for certain. The daily half of the rotation
    predicate is anchored on the file's own ctime so both processes agree
    on when the day flips (see `_rotate_by_size_or_day`).
    """
    # Audit round-2 up-security-trust P1-1: log files carry full agent exec
    # output (including anything an agent printed), so they are owner-only —
    # dir 0700 + file 0600. `logger.add` creates the file with umask-derived
    # perms, so chmod both after; rotation re-creates the file under the same
    # dir, which stays 0700 (the file itself is re-tightened on the next
    # process start).
    path.parent.mkdir(parents=True, exist_ok=True)
    with contextlib.suppress(OSError):
        path.parent.chmod(0o700)
    sink_id = logger.add(
        path,
        serialize=True,
        level="DEBUG",
        rotation=_rotate_by_size_or_day,
        retention="7 days",
    )
    with contextlib.suppress(OSError):
        path.chmod(0o600)
    return sink_id


# ─── Rollout-window quieting ────────────────────────────────────────────────
#
# User ruling 2026-08-04 (task #731): a cluster rollout is an *announced*
# operation, so its predictable side effects must not alarm at WARNING in the
# events table. While the deploy lease is held (rollout executing, or a settle
# hold waiting for stragglers — `shared.cluster_lock`), these categories are
# downgraded WARNING/ERROR → INFO in the events row; the JSONL file sink
# keeps the true level for forensics, and outside a rollout window they keep
# their original level unchanged.
#
# The list is deliberately narrow (per the ruling, no blanket WARNING
# suppression): real failures — db_pool_acquire_timeout, a genuinely stuck
# rollout (`stalled_rollout` controller), provider errors — stay loud.
_ROLLOUT_QUIET_EVENTS = frozenset(
    {
        "db_pool_acquire_slow",
        "db_outage_wait",
        "db_outage_pause",
        "db_outage_reconcile_retry",
    }
)
# event='log' lines (stdlib loggers carry no event=) matched by message prefix:
# the ops manager's per-round "blocked" / "no longer blocked" lines, and the
# `query cancellation failed: ...` line (source currently only present on some
# runner checkouts — the msg prefix is the one stable handle).
_ROLLOUT_QUIET_MSG_PREFIXES = (
    "[ops.manager] round blocked by",
    "[ops.manager] round no longer blocked after",
    "query cancellation failed",
)

# Deploy-lease read cache for the quieting check. The sink must not dial the DB
# per record, and the quieting must SURVIVE the short DB blip a rollout itself
# causes (the gateway restart drops the data plane; the lease lives in that
# same DB). One read per process per TTL; a previously-seen live lease is held
# for the whole TTL even when the next read fails, which is exactly the
# mid-rollout window the quieting exists for.
#
# The read runs on a background thread, never on the log producer's thread:
# the quieting check sits in the synchronous `_postgres_sink` on the caller's
# thread, and a lease read dials the DB (connect_timeout 5s + statement
# timeout 60s). Dialing synchronously would freeze the producer — and the
# `db_outage_*` categories the quieting exists for are emitted by the agent
# runloop's recovery loop *exactly when the DB is unreachable*, so a
# synchronous read turned the outage-pause loop into an outage amplifier
# (audit 2026-08-08 P1). The snapshot is stale by design: quieting decisions
# tolerate up to TTL staleness, blocking does not.
_DEPLOY_CACHE_TTL_S = 60.0
_deploy_cached: bool | None = None
_deploy_cached_at = 0.0
_deploy_refresh_lock = threading.Lock()


def _read_deploy_lease() -> bool:
    """One lease read: True while a cluster deploy holds the update lease.

    Runs on the cache-refresh thread. Deferred import: `shared.cluster_lock`
    imports `shared.db` imports this module at module scope, so a top-level
    import here is a hard circular-import failure (same shape as
    `_add_postgres_sink`'s deferred `import shared.db`).
    """
    from shared.cluster_lock import read_update_lease

    return read_update_lease() is not None


def _refresh_deploy_cache() -> None:
    """Refresh the lease snapshot; the caller (a daemon thread) absorbs all
    failures. An unreadable DB (mid-rollout blip) keeps the previous answer
    for the TTL — a rollout restarting the gateway must not cancel its own
    quieting; a cold cache with an unreadable DB stays None → False, the
    fail-safe direction (a warning we cannot justify quieting stays a
    warning)."""
    global _deploy_cached, _deploy_cached_at  # noqa: PLW0603 — module-level cache
    # A failed read keeps the previous answer for the TTL — DB unreachable
    # (mid-rollout blip) is the normal state of an outage this cache exists to
    # ride out, and a WARNING per minute per process would itself flood the
    # events table. Deliberately quiet.
    with contextlib.suppress(Exception):
        _deploy_cached = _read_deploy_lease()
    _deploy_cached_at = time.monotonic()


def _deploy_in_progress() -> bool:
    """True while a cluster deploy holds the update lease (rollout or settle
    hold), TTL-cached so the log sink costs at most one lease read per process
    per minute. Never raises and never blocks the caller: a stale snapshot is
    returned while a refresh runs on a background thread, so the log producer
    is never frozen by the quieting check (see the cache block above)."""
    global _deploy_cached_at  # noqa: PLW0603 — module-level cache
    now = time.monotonic()
    if now - _deploy_cached_at < _DEPLOY_CACHE_TTL_S:
        return bool(_deploy_cached)
    with _deploy_refresh_lock:
        if now - _deploy_cached_at < _DEPLOY_CACHE_TTL_S:
            return bool(_deploy_cached)
        # Claim the refresh slot before spawning the thread: concurrent
        # callers inside the TTL return the snapshot instead of stacking
        # refreshes, and a re-entrant log from inside the read itself (e.g.
        # psycopg's "query cancellation failed" line) cannot recurse.
        _deploy_cached_at = now
    threading.Thread(target=_refresh_deploy_cache, name="deploy-lease-cache", daemon=True).start()
    return bool(_deploy_cached)


def _message_to_params(
    message: loguru.Message,
) -> tuple[Any, int | None, str, str, dict[str, Any], str]:
    """Extract `(ts, agent_id, level, event, payload, source)` from one loguru record.

    Pure — no IO, no pool access — so the sync `_postgres_sink` and the
    unified emitter derive their rows from one implementation and cannot
    drift apart.

    `event` field source:
      1. record.extra["event"] (new code explicitly passes event=);
         empty string raises (caller bug, must not silently fall
         through)
      2. record.extra["label"] (compat for the existing
         `logger.info("[{label}] {body}", label="exec", ...)` pattern;
         label is treated as an event alias)
      3. "log" (bare `logger.info("foo")` without event/label; the
         jsonb payload's msg field preserves the original text so
         debug grep still works)

    `agent_id` comes from record.extra["agent_id"] — init_* must
    bind this key via `logger.configure(extra={...})` (default `"-"`
    sentinel = no agent, stored NULL). Logger calls pass
    `agent_id=N` to override the default. Missing key fast-raises
    KeyError (means init_* did not run, framework bug). The agent
    init binds a `TurnScopedAgentId` rather than a fixed id, so a
    process hosting several agents' turns attributes each record to
    the turn that wrote it.

    `source` comes from record.extra["source"] (default "system") —
    the unified events table's source column; callers that represent
    an external origin (user / self / agent:N) pass it explicitly.

    `payload` (dict, serialized to jsonb by the emitter) = record.extra
    minus dedicated columns, plus `msg` (`record.message` formatted
    text) so a single jsonb line shows the full picture during debug.

    Rollout quieting (the one deliberate impurity, task #731): a WARNING/ERROR
    record in a `_ROLLOUT_QUIET_*` category is rewritten to INFO while
    `_deploy_in_progress()` — the TTL-cached lease read — is true. Everything
    else passes through untouched."""
    record = message.record
    extra = dict(record["extra"])
    agent_id_raw = extra.pop("agent_id")  # required — init_* bound it; KeyError fast
    if isinstance(agent_id_raw, TurnScopedAgentId):
        # Deferred binding: the turn's agent, else this process's (see the class).
        agent_id_raw = agent_id_raw.resolve()
    event_explicit = extra.pop("event", None)
    if event_explicit == "":
        raise ValueError(f"empty event= passed to logger: {record['message']!r}")
    event = event_explicit or extra.get("label") or "log"
    source = extra.pop("source", "system")
    # "-" sentinel = no agent (gateway / unbound logger), maps to
    # NULL. Other values must be real agent_ids (accepts str | int;
    # int() direct cast).
    agent_id: int | None = None if agent_id_raw == "-" else int(agent_id_raw)
    extra["msg"] = record["message"]
    # When the caller uses `logger.opt(exception=True).warning(...)`
    # or `logger.exception(...)`, record["exception"] is non-empty;
    # auto-concatenate the traceback into payload so the events
    # table's jsonb also carries the stack.
    #
    # loguru sets record["exception"] to a 3-field-all-None
    # RecordException namedtuple — not Python `None` — when
    # `logger.opt(exception=True)` is called **without an active
    # exception**, meaning sys.exc_info would return three Nones.
    # Formatting that directly yields a garbage string of the form
    # NoneType: None\n (observed in 167/168 events.payload with
    # traceback NoneType: None\n and exception_value None). The
    # `exc.type is not None` guard distinguishes "real captured
    # exception" vs "loguru empty record" — empty records skip
    # writing exception_* fields, letting consumers (sidebar / SQL)
    # use `payload ? 'traceback'` to accurately identify exception
    # turns.
    exc = record["exception"]
    if exc is not None and exc.type is not None:
        extra["traceback"] = "".join(
            _traceback.format_exception(exc.type, exc.value, exc.traceback)
        )
        extra["exception_type"] = exc.type.__name__
        extra["exception_value"] = str(exc.value)
    level = record["level"].name
    if (
        level in ("WARNING", "ERROR")
        and (
            event in _ROLLOUT_QUIET_EVENTS
            or record["message"].startswith(_ROLLOUT_QUIET_MSG_PREFIXES)
        )
        and _deploy_in_progress()
    ):
        # Announced rollout side effect — see the constants block above. The
        # file sink still carries the original level; only the events row is
        # quieted.
        level = "INFO"
    return (record["time"], agent_id, level, event, extra, source)


# loguru level name -> unified `events` level (lowercase; TRACE/SUCCESS collapse
# onto the four documented levels).
_LOG_LEVELS: dict[str, str] = {
    "TRACE": "debug",
    "DEBUG": "debug",
    "INFO": "info",
    "SUCCESS": "info",
    "WARNING": "warning",
    "ERROR": "error",
    "CRITICAL": "critical",
}


def _postgres_sink(message: loguru.Message) -> None:
    """Route one loguru record into the unified event pipeline.

    This is the loguru-side adapter: field derivation lives in
    `_message_to_params`, and the event itself is enqueued to
    `shared.telemetry` — the emitter batches it into the `events` table (see
    `shared/telemetry.py`). The enqueue is non-blocking (bounded queue; shed
    records are counted and reported as `event_log_drop`), so even the
    synchronous `enqueue=False` registration costs the producer nothing.

    Derivation bugs (empty event=, unbound agent_id) raise — a caller bug
    must not silently fall through; production registrations add the handler
    with `catch=True`, which keeps the raise off the calling thread while the
    JSONL file sink still holds the line."""
    from shared import telemetry

    ts, agent_id, level, event, payload, source = _message_to_params(message)
    telemetry.emit(
        telemetry.category_for_kind(event),
        event,
        level=_LOG_LEVELS.get(level, "info"),  # type: ignore[arg-type] — get() widens to str
        agent_id=agent_id,
        source=source,
        attributes=payload,
        ts=ts,
    )


def _event_pipeline_filter(record: loguru.Record) -> bool:
    """Which loguru records may enter the unified event pipeline.

    Two families are dropped — they still reach the stderr / JSONL file sinks,
    but never become `events` rows:

      - the emitter's own failure reports (records carrying the `_no_emitter`
        marker, see `shared/telemetry.py`): a DB-down process would otherwise
        loop failure → warning → emit → failure forever;
      - `node_enter` (agent/graph/_node_log.py): a pure write-amplification
        event — zero consumers in the events table (agent_inspect reads
        `node_exit` only; death analysis reads the node_enter trail from the
        log files), yet ~15% of the stream's rows. Kept in the log files, out
        of the table.
    """
    extra = record["extra"]
    return not extra.get("_no_emitter") and extra.get("event") != "node_enter"


def _add_postgres_sink(process: str = "unknown", *, agent_id: int | None = None) -> int:
    """Eagerly open the unified event pipeline + register the loguru adapter.
    Pipeline open failure (DB unreachable / table missing etc.) raises during
    init_*; agent / gateway startup fails loud, never becomes "sink silently
    does not work" silent degrade.

    The pipeline lives in `shared.telemetry` (bounded queue + drain thread —
    the same batching/backpressure shape the former `_ThreadedPostgresSink`
    had, now owning the `events` table too). `process` names this process in
    every event row (agent-kernel / gateway / watchdog / ...); `agent_id`
    binds the default agent dimension.

    At runtime, when DB temporarily goes down (one INSERT raises), the drain
    thread drops that batch; loguru's `catch=True` on the handler ensures the
    drop doesn't surface to the caller. The JSONL file sink is the durable
    backfill source."""
    from shared import telemetry

    telemetry.init_telemetry(process=process, agent_id=agent_id)
    global _postgres_sink_id  # noqa: PLW0603 — process-level singleton
    live_handlers: Any = cast(Any, logger)._core.handlers  # private `_core` registry
    if _postgres_sink_id is not None and _postgres_sink_id in live_handlers:
        return _postgres_sink_id
    _postgres_sink_id = logger.add(
        _postgres_sink,
        level="INFO",
        enqueue=False,
        catch=True,
        filter=_event_pipeline_filter,
    )
    return _postgres_sink_id


# Process-level singleton: exec_child calls this from two paths (env
# AVA_AGENT_ID + request), and a second registration double-emits every
# loguru record (byte-identical mirror rows, task #1638). The stored id is
# re-verified against live handlers so a removed sink re-registers.
_postgres_sink_id: int | None = None


# Process-level idempotency guard. The three init_* functions add
# sinks, but `logger.add` is not idempotent — repeated calls
# accumulate sinks until fd exhaustion (errno 24 Too many open
# files). Bug symptom: the watchdog daemon calls 6 healthcheck.main()
# every 60s, each main calls init_gateway_process() -> 18 sinks per
# minute, hourly accumulation ~1000 fd then crash.
#
# Make init functions idempotent (process-level singleton) — later
# repeated calls silent-skip, matching their docstring "called once
# at startup" contract.
_init_done = False


def init_agent_process(*, agent_id: int) -> None:
    """Called once at Agent kernel process startup. Binds agent_id,
    adds stderr (human) + file (`agent-{N}.log`) + the unified event
    pipeline (events table) sinks.

    Attribution is turn-scoped: the bound `TurnScopedAgentId` resolves to the
    turn's agent when one is bound, else to `agent_id` — identical to a fixed
    binding in a one-agent process, correct in a hosted one.

    Idempotent — repeat in the same process silent-skips (see
    ``_init_done`` at module top).
    """
    global _init_done  # noqa: PLW0603 — process-level singleton
    if _init_done:
        return
    set_process_agent_id(agent_id)
    logger.configure(extra={"agent_id": TURN_SCOPED_AGENT_ID})
    logger.add(sys.stderr, format=_HUMAN_FORMAT, level="INFO", colorize=True)
    from shared.paths import logs_dir

    _add_file_sink(logs_dir() / f"agent-{agent_id}.log")
    _add_postgres_sink(process="agent-kernel", agent_id=agent_id)
    _install_stdlib_intercept()
    _init_done = True


def init_subprocess_logger(*, agent_id: int) -> None:
    """Called once at exec subprocess startup. **Only** adds the file
    sink, no stderr — the subprocess's stderr is captured by parent
    and injected into exec_output fed to the LLM; framework logs to
    stderr would pollute the agent context.

    Writes to the same file as the parent agent (`agent-{N}.log`);
    record `extra` fields distinguish kernel vs subprocess origin
    (caller binds `process_role`).

    Idempotent — repeat in the same process silent-skips (see
    ``_init_done`` at module top).
    """
    global _init_done  # noqa: PLW0603
    if _init_done:
        return
    from shared.paths import logs_dir

    logger.configure(extra={"agent_id": str(agent_id), "process_role": "subprocess"})
    _add_file_sink(logs_dir() / f"agent-{agent_id}.log")
    _install_stdlib_intercept()
    _init_done = True


def init_gateway_process(name: str = "gateway") -> None:
    """Called once at a gateway-style process startup — the gateway itself
    and every long-running service daemon (restarter / watchdog / labeler /
    memory_indexer / heartbeat / task_maintenance / ops). stderr (human) +
    file (`<name>.log`) + unified event pipeline (events table). Rows have
    agent_id NULL (extra agent_id sentinel `-`).

    `name` picks the per-daemon log file AND the process dimension of every
    event the daemon emits. Each daemon writes its own `<name>.log` so its
    logs — including an uncaught-exception traceback logged at the daemon's
    top-level except — are isolated and postmortem-able. A single shared
    gateway.log mixed every daemon by pid and made "which daemon died and
    why" hard to reconstruct.

    Idempotent — repeat in the same process silent-skips. When the
    watchdog daemon runs healthchecks reusing this function, the
    init call chain becomes "daemon init -> healthcheck main ->
    init" nested; the guard ensures subsequent calls are no-ops and
    do not accumulate sinks leading to fd leak.
    """
    global _init_done  # noqa: PLW0603
    if _init_done:
        return
    from shared.paths import logs_dir

    # Bind the deferred agent id explicitly rather than inheriting the
    # module-level default: `init_subprocess_logger` also calls
    # `logger.configure`, which REPLACES the whole extra dict, so the default is
    # not something a long-lived daemon can rely on still holding. With no turn
    # and no process agent this resolves to the `-` sentinel exactly as before;
    # in the hosted agent-runner — which inits through THIS function — it is
    # what lets each record carry the turn's agent instead of `-`.
    logger.configure(extra={"agent_id": TURN_SCOPED_AGENT_ID})
    logger.add(sys.stderr, format=_HUMAN_FORMAT, level="INFO", colorize=True)
    _add_file_sink(logs_dir() / f"{name}.log")
    _add_postgres_sink(process=name)
    _install_stdlib_intercept()
    # Capture the commit this process loaded, here at the top of its main() —
    # the earliest seam every gateway-style process shares. Deferring the
    # capture would let a checkout that moved under a long-lived daemon answer
    # on its behalf; see `shared.process_sha`.
    process_sha.freeze()
    # One structured `service_started` agent_event per gateway-style process
    # boot — the ops monitor panel's restart-count source. This function is
    # the single boot seam every long-lived service shares (gateway + all
    # daemons) and is process-idempotent, so one process = exactly one row;
    # a healthcheck that revives a dead service via Popen gives the revived
    # service its own boot row. `name` is the log-file name passed in (the
    # daemon's identity), pid/sha/host disambiguate restarts on the same box.
    logger.info(
        "service started: {name} (pid={pid}, sha={sha}, host={host})",
        event="service_started",
        name=name,
        pid=os.getpid(),
        sha=process_sha.get(),
        host=_machine_name_lazy(),
    )
    _init_done = True


def init_cli_process(*, name: str) -> None:
    """Called once at the top of a detached CLI invocation (gateway
    `spawn_update` child / watchdog schema reconcile child). Identical
    sink set to ``init_gateway_process``: stderr (human) + file
    (``<name>.log``) + unified event pipeline (agent_id NULL).

    Skipped for interactive CLI use (``ava status`` from a TTY etc.) —
    interactive output already lands on the caller's terminal and the
    extra sinks would clutter ``~/.ava/logs/`` and the ``events`` table
    with one row per command. The detection contract is "called only
    when the caller exports ``AVA_CLI_LOG_NAME``", and the caller
    decides the name (e.g. ``cli-spawn-update-<ts>``).

    Catches stdlib ``logging.getLogger`` calls from imported modules
    (uvicorn / httpx / anthropic SDK / etc.) — CLI's own ``print()``
    output still lands on stdout/stderr (which the parent's Popen
    captures into its own per-invocation log file). The events
    sink lets ``/api/cluster/admin/events`` surface a stuck
    ``ava cluster update`` child's last logged step without ssh into the host.
    """
    global _init_done  # noqa: PLW0603
    if _init_done:
        return
    from shared.paths import logs_dir

    logger.add(sys.stderr, format=_HUMAN_FORMAT, level="INFO", colorize=True)
    _add_file_sink(logs_dir() / f"{name}.log")
    _add_postgres_sink(process=name)
    _install_stdlib_intercept()
    _init_done = True
