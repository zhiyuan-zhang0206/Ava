"""Process logging composition: the loguru adapter into the unified event pipeline.

`base.log.process.start_process_logging` owns the process-level sinks and their
idempotency guard (stderr, file, stdlib intercept); this module supplies the
event-pipeline half — the loguru record derivation, the INFO sampling filter, the
sink registration — and the two entry points that compose them (`init_gateway_process`,
`init_cli_process`). `base.log` stays below `base.telemetry`: it never imports this.
"""

from __future__ import annotations

import itertools
import os
import traceback as _traceback
from collections.abc import Callable
from functools import partial
from typing import Any, Protocol, cast

import loguru

from base.log import logger
from base.log.process import start_process_logging
from base.log.sinks import add_sink
from base.telemetry.delivery.pipeline import EventPipeline
from base.telemetry.emitter import category_for_kind, emit, init_telemetry

__all__ = ["LoadedImage", "add_event_sink", "init_cli_process", "init_gateway_process"]

# The one host-stall companion whose EVENT level is rewritten: psycopg logs
# its own `query cancellation failed: ...` WARNING when a `_try_cancel`
# outlives its timeout — the query cancel rides the same unresponsive
# connection the stall wedged, so it lands beside the interrupt watcher's
# abandon report (2026-10-03 triage #13: ~16 such pairs in 24h, not a bug).
# The emitted event drops to info; every file sink keeps psycopg's true
# WARNING for forensics. Matched by message prefix: by derivation time the
# record names neither the library nor a category, and the prefix is the one
# stable handle.
_COMPANION_MSG_PREFIXES = ("query cancellation failed",)


class LoadedImage(Protocol):
    """What a gateway-style process boot stamps on its events: the loaded source commit.

    `base.native_process.loaded_commit.LoadedCommit` is the production capture;
    `log` is below `native_process` and reads only the `sha`.
    """

    @property
    def sha(self) -> str | None: ...


def _message_to_params(
    message: loguru.Message,
) -> tuple[Any, int | None, str, str, dict[str, Any], str]:
    """Extract `(ts, agent_id, level, event, payload, source)` from one loguru record.

    Pure — no IO, no pool access — so the sync `_event_sink` and the
    unified emitter derive their rows from one implementation and cannot
    drift apart.

    `event` field source:
      1. record.extra["event"] (callers pass event= explicitly);
         empty string raises (caller bug, must not silently fall
         through)
      2. "log" (bare `logger.info("foo")` without event=; the
         jsonb payload's msg field preserves the original text so
         debug grep still works)

    `agent_id` comes from record.extra["agent_id"] — init_* must
    bind this key via `logger.configure(extra={...})` (default `"-"`
    sentinel = no agent, stored NULL). Logger calls pass
    `agent_id=N` to override the default. Missing key fast-raises
    KeyError (means init_* did not run, framework bug). Shared services
    default to no agent; a disposable exec process binds its own identity.

    `source` comes from record.extra["source"] (default "system") —
    the unified event stream's `source` field; callers that represent
    an external origin (user / self / agent:N) pass it explicitly.
    A record with ``transport_source`` instead keeps its ``source`` extra in
    the payload and uses ``transport_source`` for the field.

    `payload` (dict, serialized to jsonb by the emitter) = record.extra
    minus dedicated columns, plus `msg` (`record.message` formatted
    text) so a single jsonb line shows the full picture during debug."""
    record = message.record
    extra = dict(record["extra"])
    agent_id_raw = extra.pop("agent_id")  # required — init_* bound it; KeyError fast
    event_explicit = extra.pop("event", None)
    if event_explicit == "":
        raise ValueError(f"empty event= passed to logger: {record['message']!r}")
    event = event_explicit or "log"
    transport_source = extra.pop("transport_source", None)
    source = transport_source if transport_source is not None else extra.pop("source", "system")
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
    if level == "WARNING" and record["message"].startswith(_COMPANION_MSG_PREFIXES):
        # A host-stall companion, not its own signal (see the constant above).
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


def _event_sink(
    message: loguru.Message, *, producer: Callable[[], EventPipeline] | None = None
) -> None:
    """Route one loguru record into the unified event pipeline.

    This is the loguru-side adapter: field derivation lives in
    `_message_to_params`, and the event itself is enqueued to
    `base.telemetry` — the emitter batches it into the unified event stream
    (see `base/telemetry/emitter.py`). The enqueue is non-blocking (bounded queue; shed
    records are counted and reported as `event_log_drop`), so even the
    synchronous `enqueue=False` registration costs the producer nothing.

    Derivation bugs (empty event=, unbound agent_id) raise — a caller bug
    must not silently fall through; production registrations add the handler
    with `catch=True`, which keeps the raise off the calling thread while the
    JSONL file sink still holds the line."""
    ts, agent_id, level, event, payload, source = _message_to_params(message)
    emit(
        category_for_kind(event),
        event,
        level=_LOG_LEVELS.get(level, "info"),  # type: ignore[arg-type] — get() widens to str
        agent_id=agent_id,
        source=source,
        attributes=payload,
        ts=ts,
        producer=producer,
    )


# Level-graded sampling for bare INFO `log` records (task #1637).
_LOG_INFO_SAMPLE_EVERY = 10
_log_info_sample_counter = itertools.count()


def _event_pipeline_filter(record: loguru.Record) -> bool:
    """Which loguru records may enter the unified event pipeline.

    Three families are dropped or thinned (they still reach the stderr /
    JSONL file sinks, but never or rarely become `events` rows):

      - the emitter's own failure reports (records carrying the `_no_emitter`
        marker, see `base/telemetry/emitter.py`): a mirror-down process would
        otherwise loop failure → warning → emit → failure forever;
      - `node_enter` (agent/graph/node_log.py): a pure write-amplification
        event — zero consumers in the event stream (agent_inspect reads
        `node_exit` only; death analysis reads the node_enter trail from the
        log files), yet ~15% of the stream's rows. Kept in the log files, out
        of the stream.
      - bare INFO `log` records: one in `_LOG_INFO_SAMPLE_EVERY` passes; WARNING+ pass.
    """
    extra = record["extra"]
    if extra.get("_no_emitter") or extra.get("event") == "node_enter":
        return False
    # Known artifact of claim_idle_wait_span (base/telemetry/tracing.py): the traceloop
    # LangChain handler sets `gen_ai.task.status` on the claim node span AFTER
    # we end it early at the idle-park boundary, so the OTel SDK logs one
    # WARNING ("Setting attribute on ended span.") per park. The warning is
    # information-free (the span end is deliberate) but would add one junk
    # events row per idle park; keep it in the log-file sinks, drop it from
    # the table. If the handler stops writing to ended spans, the row stops
    # appearing entirely and this sentinel can be removed.
    if record.get("message") == "Setting attribute on ended span.":
        return False
    level = getattr(record.get("level"), "name", "")  # missing level -> "" (unsampled)
    if level == "INFO" and (extra.get("event") or "log") == "log":
        return next(_log_info_sample_counter) % _LOG_INFO_SAMPLE_EVERY == 0
    return True


def add_event_sink(
    process: str = "unknown",
    *,
    agent_id: int | None = None,
    producer: Callable[[], EventPipeline],
    machine_reader: Callable[[], str],
) -> int:
    """Eagerly open the unified event pipeline + register the loguru adapter.
    Pipeline open failure raises during init_* (it no longer touches the DB);
    agent / gateway startup fails loud, never becomes "sink silently
    does not work" silent degrade.

    Public for a boot seam that composes its own sinks instead of calling an
    init_*: the exec child (`agent/execution/child.py`) pairs
    `init_subprocess_logger` with this, best-effort, so a pipeline outage
    degrades it to the file sink rather than stopping agent code.

    The pipeline lives in `base.telemetry` (bounded queue + drain thread —
    the same batching/backpressure shape the former `_ThreadedPostgresSink`
    had, now the single write path for every sink). `process` names this process in
    every event row (gateway / watchdog / agent-exec / ...); `agent_id`
    binds the default agent dimension.

    At runtime, sink failures are contained on the drain thread; loguru's
    `catch=True` on the handler ensures they don't surface to the caller. The
    JSONL file sink is the durable backfill source."""
    init_telemetry(
        process=process,
        agent_id=agent_id,
        pipeline=producer(),
        machine_reader=machine_reader,
    )
    global _event_sink_id  # noqa: PLW0603 — process-level singleton
    live_handlers: Any = cast(Any, logger)._core.handlers  # private `_core` registry
    if _event_sink_id is not None and _event_sink_id in live_handlers:
        return _event_sink_id
    _event_sink_id = add_sink(
        partial(_event_sink, producer=producer),
        level="INFO",
        enqueue=False,
        catch=True,
        filter=_event_pipeline_filter,
    )
    return _event_sink_id


# Process-level singleton: exec_child calls this from two paths (env
# AVA_AGENT_ID + request), and a second registration double-emits every
# loguru record (byte-identical mirror rows, task #1638). The stored id is
# re-verified against live handlers so a removed sink re-registers.
_event_sink_id: int | None = None


def init_gateway_process(
    name: str = "gateway",
    *,
    producer: Callable[[], EventPipeline],
    machine_reader: Callable[[], str],
    image: LoadedImage,
) -> None:
    """Called once at a gateway-style process startup — the gateway itself
    and every long-running service daemon (agent-host / watchdog / labeler /
    memory_indexer / heartbeat / task_maintenance / ops). stderr (human) +
    file (`<name>.log`) + unified event pipeline. Rows have
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

    def announce() -> None:
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
            sha=image.sha,
            host=machine_reader(),
        )

    start_process_logging(
        name,
        attach_events=partial(
            add_event_sink, process=name, producer=producer, machine_reader=machine_reader
        ),
        announce=announce,
    )


def init_cli_process(
    *, name: str, producer: Callable[[], EventPipeline], machine_reader: Callable[[], str]
) -> None:
    """Called once by the CLI verbs that bring a unit up. Identical sink set to ``init_gateway_process``,
    minus its ``service_started`` row: stderr (human) + file (``<name>.log``)
    + unified event pipeline (agent_id NULL).

    The CLI's verbs are the keys of ``cli.main._CLI_LOG_NAMES`` (``start``,
    ``restart``, ``lgtm on|off``), which also name each
    one's file; ``cli.main`` calls this before dispatch. Every other verb
    (``ava status`` etc.) opens no sink — its output already lands on the
    caller's terminal, and the extra sinks would clutter ``logs/`` and the
    event stream with one row per command.

    Catches stdlib ``logging.getLogger`` calls from imported modules
    (uvicorn / httpx / anthropic SDK / etc.) — CLI's own ``print()``
    output still lands on stdout/stderr (which the parent's Popen
    captures into its own per-invocation log file). The events
    sink lets ``/api/cluster/admin/events`` surface a stuck supervised
    CLI child's last logged step without ssh into the host.
    """
    start_process_logging(
        name,
        attach_events=partial(
            add_event_sink, process=name, producer=producer, machine_reader=machine_reader
        ),
    )
