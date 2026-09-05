"""Standard-library interception and local loguru sinks."""

from __future__ import annotations

import contextlib
import datetime
import logging
import sys
from pathlib import Path
from typing import Any

from loguru import logger


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


# Top-level logger names for our own code. `_install_stdlib_intercept` raises
# these back to DEBUG so a controller's
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

    # A prior default Uvicorn config installs child and parent handlers,
    # disables their propagation, and changes levels. Clear that configuration
    # too: otherwise uvicorn.error bypasses this root intercept or drops ERROR
    # records before they reach it. The gateway passes log_config=None, but init
    # must be idempotent in a process whose logging was configured before it
    # started (#970).
    for uvicorn_logger_name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        uvicorn_logger = logging.getLogger(uvicorn_logger_name)
        uvicorn_logger.handlers.clear()
        uvicorn_logger.propagate = True
        uvicorn_logger.setLevel(logging.NOTSET)

    # uvicorn.access logs one INFO line per HTTP request — with the gateway's
    # uvicorn.run(log_config=None) those would flood the file sink and the
    # events table (a busy gateway emits thousands/day). Gate to WARNING:
    # only abnormal access records survive. uvicorn.error inherits the root
    # INFO floor — that logger carries startup
    # lines AND the unhandled-ASGI-exception tracebacks #970 exists to
    # capture ("Exception in ASGI application ..." with the full stack).
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)


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
