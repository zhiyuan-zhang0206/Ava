"""Trace recording — OTLP/HTTP export to the local OTel Collector sidecar.

**Record** (this module): called once per process early in `agent/loop.py:main()`.
When `trace_enabled` (default on), OpenLLMetry (traceloop-sdk) auto-instruments
the Anthropic/OpenAI/Google SDKs + LangChain/LangGraph, and every span is
exported over OTLP/HTTP (protobuf wire format) to the local OTel Collector
sidecar (`AVA_TELEMETRY_OTLP_ENDPOINT`, default 127.0.0.1:4318) by
`OtlpJsonHttpSpanExporter`; the slow import+init runs on a daemon thread off
the boot path (use sites wait via ensure_init_resolved()). The sidecar (one
per machine, supervised like every other Ava service) writes the local JSONL
mirror under `$AVA_HOME/traces/` via its file exporter. A gateway sidecar fans out to loopback Tempo; a pure runner
sidecar relays to the gateway collector's authenticated private receiver. Each
line is one standard OTLP/JSON `ExportTraceServiceRequest` (the same wire shape
any OTLP backend ingests), giving accepted batches a vendor-neutral, grep-able
local recovery copy. A gap present in the mirror can be replayed with
`ava trace ship`.

  **Metadata-only by default (trace v2, 2026-08-05)**: spans record chain
  metadata (span names, langgraph paths, checkpoint refs, agent_id, durations,
  status, trace_id) but NOT LLM content. Content stripping happens at two
  layers: `TRACELOOP_TRACE_CONTENT=false` stops the instrumentors from
  attaching prompt/completion attributes at the source (2026-08-05 incident:
  31 GB/h of mirror, 99.89% of it `gen_ai.task.input/output`-style content),
  and `OtlpJsonHttpSpanExporter` re-strips defensively before anything leaves
  the process (a future instrumentor that ignores the env var cannot leak
  content back in). Turn content is fetched on demand from the checkpoints
  table by trace id — see `shared/checkpoint.py` / the gateway trace endpoint.

- **Ship** (`cli/commands/trace.py`, `ava trace ship`): replays a time window of
  the sidecar's JSONL mirror to Tempo directly on a gateway, or through the
  gateway collector's authenticated receiver on a pure runner. This is the
  recovery path (backend down longer than the collector queue held, offline
  machines) — the live fan-out is the collector's job. It is resumable via a
  watermark, and it gates on `telemetry_otlp_enabled` — one kill switch for
  the whole OTLP surface (with the sidecar architecture this also stops
  recording, since recording IS the OTLP export).

turn_span() in agent/_runloop.py wraps each graph.ainvoke — one invocation =
one agent TURN — in a native OTel root span so every LLM/tool child span of
that turn nests under it. The root is a PLACEHOLDER: ended (and thus
exported) at turn START, so a trace always has its root even when the process
is killed mid-turn (#1964); children export as they end and parent under the
already-ended root via the kept OTel context. The
root carries the vendor-neutral `session.id` (Tempo/Grafana group one agent's
turns into a session by it) plus `ava.turn` (the per-process turn counter).

claim_idle_wait_span() (used by the agent claim node) ends the open LangChain
node span (`execute_task claim`) before the node parks in
`agent/graph/_claim_batch._wait_for_batch` and records the idle park as an
explicit `claim idle-wait` span, so an idle wait shows as a labeled span
instead of a giant opaque node span in the trace.

Idempotent: the _initialized guard prevents a second init. A collector miss at
startup logs once, then one daemon loop retries every five minutes until trace
recording comes up (or the single arm attempt fails permanently); disk-watermark auto-degrade remains a deliberate no-retry.

History: Laminar -> Langfuse 2026-06-06; Langfuse -> Tempo (LGTM) 2026-08-11;
agent-side mirror -> local collector sidecar (task #1266) 2026-08-14.
"""

from __future__ import annotations

import contextlib
import json
import os
import threading
import time
from collections.abc import Callable, Generator, Sequence
from functools import wraps
from typing import Any, cast

import httpx
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace import Span as SdkSpan
from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult

from shared.config import settings
from shared.log import logger
from shared.observability import cluster_label
from shared.paths import traces_dir
from shared.telemetry_otlp import (
    COLLECTOR_RETRY_INTERVAL_S,
    _observability_export_allowed,
    endpoint_reachable,
)
from shared.trace_mirror import (
    _disk_usage,
    _disk_watermark_exceeded,
    _enforce_dir_cap,
    _gzip_old_mirror,
    _mirror_day,
    _mirror_epoch,
    _mirror_size,
    _mirror_sort_key,
    _prune_old_mirror,
)

# Mirror-hygiene re-exports: moved to shared/trace_mirror.py 2026-08-30 and
# re-exported here for the existing test/caller surface.
__all__ = [
    "OtlpJsonHttpSpanExporter",
    "_disk_usage",
    "_disk_watermark_exceeded",
    "_enforce_dir_cap",
    "_gzip_old_mirror",
    "_mirror_day",
    "_mirror_epoch",
    "_mirror_size",
    "_mirror_sort_key",
    "_prune_old_mirror",
    "claim_idle_wait_span",
    "ensure_init_resolved",
    "initialize_tracing",
    "turn_span",
]

# Span attribute keys.
#
# `session.id` — vendor-neutral session grouping key (emerging GenAI semantic
# convention); the consumer groups one agent's spans into a session by it.
_ATTR_NEUTRAL_SESSION_ID = "session.id"

# `ava.turn` — the per-process turn counter on each turn's root span; orders
# an agent's turns within one process lifetime (it restarts at 1 on respawn;
# cross-process ordering comes from timestamps / checkpoint refs).
_ATTR_TURN = "ava.turn"

# LangChain/LangGraph node-span naming from the traceloop callback handler:
# every graph node span is `execute_task <node>`. claim_idle_wait_span is only
# allowed to end a span carrying this prefix — inside the claim node that is
# the node's own `execute_task claim` span; ending any other span (the
# enclosing workflow/root span, when the instrumentor is not attached) would
# truncate the turn trace, so the prefix check is the fail-safe boundary.
_NODE_SPAN_PREFIX = "execute_task "

# Explicit span name for the claim node's idle park: keeps the wait visible
# and attributable instead of hiding it inside the node span's duration.
_IDLE_WAIT_SPAN_NAME = "claim idle-wait"


# ── LLM content stripping (trace v2) ─────────────────────────────────────────
#
# Span attributes that carry LLM content — the 99.89% of the 2026-08-05 mirror
# blowup (31 GB/h with AVA_TRACE_ENABLED=true). Chain metadata lives in the
# `traceloop.association.properties.*` / `gen_ai.task.*` / `langgraph.*`
# attributes and never appears here; these keys are pure payload.
#
# Blacklist (exact keys) + prefix rules (gen_ai.input.* / gen_ai.output.*):
# the openllmetry instrumentors emit prompts/completions under gen_ai.* per
# the GenAI semantic convention; traceloop's wrapper mirrors them as
# traceloop.entity.*.
_CONTENT_ATTR_KEYS: frozenset[str] = frozenset(
    {
        "gen_ai.task.input",
        "gen_ai.task.output",
        "gen_ai.system_instructions",
        "gen_ai.tool_calls",
        "traceloop.entity.input",
        "traceloop.entity.output",
        "messages",
        "system_instructions",
        "inputs",
        "outputs",
    }
)
_CONTENT_ATTR_PREFIXES: tuple[str, ...] = (
    "gen_ai.input.",
    "gen_ai.output.",
)

# Defensive size guard: a metadata attribute is tiny (names, ids, paths,
# durations); anything bigger on a single string value is treated as content
# even if an instrumentor invents a new key — bounds a future leak to a few
# KB per span. 16 KB is above the largest real metadata (a long langgraph
# path array) and far below the smallest real content (a prompt).
_MAX_ATTR_STRING_CHARS = 16 * 1024


def _is_content_attribute(key: str) -> bool:
    """True when an attribute key carries LLM content and must not reach disk."""
    if key in _CONTENT_ATTR_KEYS:
        return True
    return any(key.startswith(p) for p in _CONTENT_ATTR_PREFIXES)


def _attribute_value_size(value: dict[str, Any]) -> int:
    """Approximate serialized size of one OTLP AnyValue dict (chars of the
    dominant string payload). Used by the size guard; a content-free
    attribute is a few hundred chars at most."""
    for field in ("stringValue", "bytesValue", "intValue", "doubleValue", "boolValue"):
        if field in value:
            return len(str(value[field]))
    if "arrayValue" in value:
        return sum(_attribute_value_size(v) for v in value["arrayValue"].get("values", []))
    if "kvlistValue" in value:
        return sum(
            _attribute_value_size(kv.get("value", {})) + len(kv.get("key", ""))
            for kv in value["kvlistValue"].get("values", [])
        )
    return 0


def _strip_content_attributes(otlp: dict[str, Any]) -> None:
    """Remove LLM-content attributes from a decoded OTLP export request, in place.

    Walks resourceSpans -> scopeSpans -> spans (attributes + events) and drops
    every attribute whose key is a known content key (or content prefix), plus
    any attribute whose string payload exceeds `_MAX_ATTR_STRING_CHARS` (the
    size guard — metadata is never large). Span names, timestamps, status,
    ids and `traceloop.*` / `gen_ai.task.*` / `langgraph.*` metadata survive.
    """
    for rs in otlp.get("resourceSpans", []):
        for ss in rs.get("scopeSpans", []):
            for span in ss.get("spans", []):
                kept: list[dict[str, Any]] = [
                    kv
                    for kv in span.get("attributes", [])
                    if not _is_content_attribute(kv.get("key", ""))
                    and _attribute_value_size(kv.get("value", {})) <= _MAX_ATTR_STRING_CHARS
                ]
                if kept:
                    span["attributes"] = kept
                else:
                    span.pop("attributes", None)
                for ev in span.get("events", []):
                    ev_kept: list[dict[str, Any]] = [
                        kv
                        for kv in ev.get("attributes", [])
                        if not _is_content_attribute(kv.get("key", ""))
                        and _attribute_value_size(kv.get("value", {})) <= _MAX_ATTR_STRING_CHARS
                    ]
                    if ev_kept:
                        ev["attributes"] = ev_kept
                    else:
                        ev.pop("attributes", None)


# Module-level mutable state for the idempotent init guard and collector retry.
# Dict mutation avoids ruff PLW0603 (global statement) while keeping
# the same observable semantics without global assignment statements.
_state: dict[str, Any] = {
    "initialized": False,
    "collector_offline_reported": False,
    "retry_thread": None,
    "arm_thread": None,
    "init_resolved": threading.Event(),
    "arm_failed": False,
    "timeout_reported": False,
}
_init_lock = threading.Lock()


class OtlpJsonHttpSpanExporter(SpanExporter):
    """OTLP/HTTP span exporter to the local OTel Collector (protobuf wire).

    Each `export()` batch is encoded to the OTLP dict, content-stripped
    (defensive layer 2 — content never leaves the process), re-serialized to
    the OTLP `ExportTraceServiceRequest` protobuf and POSTed to
    `{endpoint}/v1/traces` with `Content-Type: application/x-protobuf` — the
    same wire path `ava trace ship` uses. Protobuf, not OTLP/JSON, on the
    wire: the collector's JSON receiver rejects the SDK's padded-base64 ids
    (verified 2026-08-14 against otelcol-contrib 0.155.0). The endpoint is
    the LOCAL sidecar (``AVA_TELEMETRY_OTLP_ENDPOINT``, default
    127.0.0.1:4318); agents never dial a backend directly.

    The sidecar is supervised (watchdog, 60s), so a failed POST means a
    restart in progress or a broken install: retry briefly, then return
    FAILURE and let the SDK's batch processor drop the batch — bounded loss,
    reported. The mirror is the collector's job now; this exporter's only
    duties are stripping (defensive layer 2) and the POST itself.
    """

    # POST attempts per batch before FAILURE. The sidecar is local; 3 tries
    # with short backoff covers a watchdog restart without stalling the batch
    # processor's schedule for long.
    _MAX_ATTEMPTS = 3

    def __init__(self, endpoint: str | None = None) -> None:
        base = (endpoint or settings.observability.telemetry_otlp_endpoint).rstrip("/")
        self._endpoint = f"{base}/v1/traces"
        self._failures = 0

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        # Imported lazily: the OTLP encoder pulls in the protobuf stack, which
        # the no-tracing path should not pay for.
        from google.protobuf.json_format import MessageToDict, Parse
        from opentelemetry.exporter.otlp.proto.common.trace_encoder import encode_spans

        request = encode_spans(spans)
        otlp = MessageToDict(request)
        # Defensive layer 2 of content stripping (layer 1 is
        # TRACELOOP_TRACE_CONTENT=false at init): whatever the instrumentors
        # attached, content never leaves the process. `trace_strip_content=False`
        # opts out (benchmarks that genuinely want full prompts in the mirror).
        if settings.observability.trace_strip_content:
            _strip_content_attributes(otlp)
        # dict -> protobuf: the same reconstruction `ava trace ship` performs
        # on mirror lines (Parse of the OTLP/JSON dict, then the binary form).
        from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
            ExportTraceServiceRequest,
        )

        req = Parse(
            json.dumps(otlp, separators=(",", ":"), ensure_ascii=False), ExportTraceServiceRequest()
        )
        body = req.SerializeToString()
        headers = {"Content-Type": "application/x-protobuf"}
        for attempt in range(self._MAX_ATTEMPTS):
            try:
                resp = httpx.post(self._endpoint, content=body, headers=headers, timeout=5.0)
                resp.raise_for_status()
                self._failures = 0
                return SpanExportResult.SUCCESS
            except Exception as exc:  # exporter must never raise into the SDK
                self._failures += 1
                if attempt < self._MAX_ATTEMPTS - 1:
                    time.sleep(0.5 * (attempt + 1))
                    continue
                # Report first + every 50th consecutive failure — a sustained
                # collector outage is an ops signal, a transient blip is not.
                if self._failures == 1 or self._failures % 50 == 0:
                    logger.warning(
                        "OTLP trace export failed — collector unreachable",
                        event="trace",
                        action="export_failed",
                        endpoint=self._endpoint,
                        consecutive_failures=self._failures,
                        error=repr(exc),
                    )
                return SpanExportResult.FAILURE
        return SpanExportResult.FAILURE  # pragma: no cover — loop always returns

    def force_flush(self, timeout_millis: int = 30_000) -> bool:  # noqa: ARG002 — SpanExporter interface signature
        return True

    def shutdown(self) -> None:
        return None


def _retry_initialize_tracing() -> None:
    """Retry the collector preflight on one daemon thread until init succeeds
    or fails permanently (arm_failed — one arm attempt per process)."""
    while not _state["initialized"] and not _state["arm_failed"]:
        time.sleep(COLLECTOR_RETRY_INTERVAL_S)
        if _state["initialized"] or _state["arm_failed"]:
            return
        initialize_tracing()


def _start_collector_retry() -> None:
    retry_thread = _state["retry_thread"]
    if isinstance(retry_thread, threading.Thread) and retry_thread.is_alive():
        return
    retry_thread = threading.Thread(
        target=_retry_initialize_tracing,
        daemon=True,
        name="trace-collector-retry",
    )
    _state["retry_thread"] = retry_thread
    retry_thread.start()


def _arm_tracing(endpoint: str) -> None:
    """Import traceloop and run Traceloop.init, on one daemon thread.

    The import + init cost ~2.5-3 s (traceloop.sdk pulls in pandas via its
    datasets client; the GOOGLE_GENERATIVEAI instrumentor imports the ~66 MB
    google.genai that ava_builtins/plugins/lm_google/gemini_cache.py keeps lazy). Off the boot
    path, initialize_tracing returns after the cheap decisions; consumers
    wait on ensure_init_resolved() before first use; a failure logs and
    resolves the wait — recording stays off instead of killing boot.
    """
    try:
        # Content stripping, layer 1 (the source): false-before-init keeps
        # LLM content out of span attributes. Unconditional when
        # strip_content is on — an operator-set true would re-open the
        # 31 GB/h hole. Layer 2 (the exporter) stays armed regardless.
        if settings.observability.trace_strip_content:
            os.environ["TRACELOOP_TRACE_CONTENT"] = "false"

        from traceloop.sdk import Traceloop
        from traceloop.sdk.instruments import Instruments

        from shared.observability import production_identity

        # Mirror the LLM call paths Ava actually uses. LangGraph rides on the
        # LANGCHAIN instrumentor (its callback handler nests node spans), so
        # there is no separate LANGGRAPH instrument. LiteLLM, when used,
        # surfaces through the provider instrumentors at the SDK layer.
        instruments = {
            Instruments.ANTHROPIC,
            Instruments.OPENAI,
            Instruments.LANGCHAIN,
            Instruments.GOOGLE_GENERATIVEAI,
        }

        # `exporter=` makes OtlpJsonHttpSpanExporter the sole span exporter,
        # pointed at the LOCAL sidecar (no api_endpoint/api_key, so the SDK
        # never dials a remote backend itself); `telemetry_enabled=False`
        # stops traceloop's own usage telemetry from phoning home.
        #
        # Idempotence guard, under the init lock: at most one Traceloop.init per
        # process. A second arm thread is only reachable when the module state
        # was reset while the first was still in flight (the tests reset _state
        # between cases; production never does), but once two arm threads run,
        # the SDK's TracerWrapper singleton makes a second init FAKE-SUCCEED
        # without adding instrumentors — and in tests the second call lands in
        # the next case's monkeypatch (the #1065 / post-#1068 flake: a second
        # call counted by test_idempotent_second_call_is_noop). The check runs
        # after the slow import on purpose: both threads serialize on the
        # import lock, so the loser reaches here after the winner has recorded
        # its outcome.
        with _init_lock:
            if _state["initialized"] or _state["arm_failed"]:
                return
            try:
                Traceloop.init(  # pyright: ignore[reportUnknownMemberType]
                    app_name="ava",
                    exporter=OtlpJsonHttpSpanExporter(endpoint=endpoint),
                    instruments=instruments,
                    disable_batch=False,
                    telemetry_enabled=False,
                    resource_attributes={
                        "cluster": cluster_label(),
                        "service.line": "ava",
                        "environment": "prod" if production_identity() else "dev",
                    },
                )
                logger.info(
                    "trace recording enabled",
                    event="trace",
                    endpoint=endpoint,
                    mirror_dir=str(traces_dir()),
                )
                _state["collector_offline_reported"] = False
                _state["initialized"] = True
            except Exception:
                _state["arm_failed"] = True
                raise
    except BaseException as exc:
        # One attempt per process: the retry loop only bridges
        # unreachable->reachable, and retrying Traceloop.init after a partial
        # failure would fake-succeed — the TracerWrapper singleton survives,
        # so a second init() adds no instrumentors yet reports success.
        #
        # BaseException (a SystemExit/GeneratorExit escape from inside the
        # SDK — not expected, but the one-attempt contract must hold for
        # however the thread dies): with only `except Exception`, a dead
        # arm_thread carrying no flag gets past _start_arm_thread's
        # is_alive() guard and a later initialize_tracing() would re-arm —
        # the one-attempt-per-process contract was bypassable in a corner.
        # Mark the attempt spent; this daemon thread has no caller to
        # propagate to, so swallow and log.
        _state["arm_failed"] = True
        logger.warning(
            "trace recording failed to initialize — spans disabled this process",
            event="trace",
            action="recording_init_failed",
            error=repr(exc),
        )
    finally:
        _state["init_resolved"].set()


def _start_arm_thread(endpoint: str) -> None:
    """Spawn (once) the daemon thread that imports traceloop and arms it."""
    arm_thread = _state["arm_thread"]
    if isinstance(arm_thread, threading.Thread) and arm_thread.is_alive():
        return
    arm_thread = threading.Thread(
        target=_arm_tracing,
        args=(endpoint,),
        daemon=True,
        name="trace-arm",
    )
    _state["arm_thread"] = arm_thread
    arm_thread.start()


_INIT_RESOLVED_TIMEOUT_S = 30.0


def ensure_init_resolved() -> None:
    """Block until the boot-time trace decision is final; no-op when none is pending.

    turn_span() calls this before opening the first turn root span: a span
    opened against the unset proxy tracer is silently lost, and LangChain
    callback managers configured before the wrap lands never carry the
    traceloop handler. Only waits when this process armed background tracing
    — declined or never-requested init returns immediately.

    The wait is bounded (_INIT_RESOLVED_TIMEOUT_S): a hung arm (cross-thread
    import deadlock, SDK network stall) must not hang the first turn forever.
    A timeout logs once, proceeds without recording, and later turns skip the
    wait entirely — if the arm eventually completes, recording comes up
    mid-life (the first turn's spans stay lost, the price of a hung init).
    """
    if _state["arm_thread"] is None or _state["timeout_reported"]:
        return
    if _state["init_resolved"].wait(_INIT_RESOLVED_TIMEOUT_S):
        return
    _state["timeout_reported"] = True
    logger.warning(
        "trace init not resolved within timeout — proceeding without recording",
        event="trace",
        action="init_resolved_timeout",
        timeout_s=_INIT_RESOLVED_TIMEOUT_S,
    )


def _serialized_initialize(function: Callable[[], None]) -> Callable[[], None]:
    """Apply the module init lock while preserving the public docstring."""

    @wraps(function)
    def serialized() -> None:
        with _init_lock:
            function()

    return serialized


@_serialized_initialize
def initialize_tracing() -> None:
    """Initialize OTel span recording to the local collector. Idempotent.

    No-op when settings.observability.trace_enabled=False. When on, this call
    runs only the cheap decisions — disk guards + collector preflight — and
    hands the heavy part (traceloop import + Traceloop.init, ~2.5-3 s) to one
    daemon thread (_arm_tracing), one attempt per process. The boot path is
    therefore sub-second; the ordering contract "instrumentors installed
    before the first turn" is enforced by ensure_init_resolved() inside
    turn_span (the callback-manager wrap and the SDK instrumentors are
    call-time, so a model built during boot still produces spans). Spans:
    OtlpJsonHttpSpanExporter pointed at the LOCAL sidecar, traceloop
    instrumenting Anthropic + OpenAI + LangChain + Google GenAI (LangGraph
    via the LangChain callback handler).

    Recording is one OTLP/HTTP hop to the LOCAL sidecar; the sidecar writes the
    JSONL mirror and either fans out locally or relays to the gateway. If the
    sidecar itself is not answering at init, recording stays off temporarily:
    the episode is logged once and one daemon loop re-checks every five
    minutes until init succeeds or the single arm attempt fails
    permanently (one arm attempt per process).

    Guards (each independently configurable):
    - compression (`_gzip_old_mirror`): rotated (non-active) mirror segments
      gzipped first, so retention and the cap see the compressed footprint.
    - retention (`trace_retention_days`): day-stamped mirror files older than
      N days pruned first (the collector's rotation also bounds them).
    - directory cap (`trace_max_dir_mb`): oldest mirror files deleted until
      the directory fits under the cap. These three run BEFORE the watermark
      guard, so an over-watermark disk still gets its relief pass.
    - disk watermark (`trace_disk_watermark`): data disk over the fraction ->
      recording skipped, warning event emitted (auto-degrade).
    - content stripping (`trace_strip_content`): TRACELOOP_TRACE_CONTENT=false
      + exporter-side re-strip, so the mirror holds metadata only.
    """
    if _state["initialized"] or _state["arm_failed"]:
        return
    if not settings.observability.trace_enabled:
        return
    if not _observability_export_allowed():
        return

    # Bounded disk FIRST — before the watermark guard, so the relief valves
    # still run when recording is auto-degraded: compress old segments (so
    # the cap and the day retention see the post-compression footprint),
    # then retention (days), then the hard directory cap. When the disk is
    # already over the watermark this is exactly the pass that frees space —
    # skipping it there would let an over-watermark disk stay full.
    _gzip_old_mirror()
    _prune_old_mirror(settings.observability.trace_retention_days)
    _enforce_dir_cap(settings.observability.trace_max_dir_mb)

    # Disk-watermark guard: if the data disk is over its watermark, skip
    # recording entirely (auto-degrade) instead of letting the mirror grow
    # the disk toward full. Logged as a warning event so the degradation is
    # visible; a later agent start re-checks.
    usage = _disk_usage()
    if usage is not None and usage[0] > settings.observability.trace_disk_watermark:
        # Auto-degrade: skip recording so the mirror can never fill the disk.
        # The loguru events sink lands this in the unified events table
        # (event_name='trace', action='recording_disabled_disk_watermark') —
        # visible to ops, never crashes. The warning carries the measured
        # numbers so triage does not need a second command.
        logger.warning(
            "trace recording disabled — data disk over watermark",
            event="trace",
            action="recording_disabled_disk_watermark",
            watermark=settings.observability.trace_disk_watermark,
            usage_fraction=round(usage[0], 3),
            free_gb=round(usage[1] / (1024**3), 1),
        )
        return

    # Local-collector preflight: recording IS an OTLP export now, so a missing
    # sidecar means no recording (and no mirror — the mirror is written by the
    # collector). Skip with a warning event instead of arming an exporter that
    # fails every batch. One daemon retry loop re-checks at the exporter retry
    # cadence, while repeated callers in the same episode stay quiet.
    endpoint = settings.observability.telemetry_otlp_endpoint
    if not endpoint_reachable(endpoint):
        if not _state["collector_offline_reported"]:
            logger.warning(
                "trace recording disabled — local OTel collector not answering",
                event="trace",
                action="recording_disabled_collector_unreachable",
                endpoint=endpoint,
            )
            _state["collector_offline_reported"] = True
        _start_collector_retry()
        return

    # The heavy part — traceloop import + Traceloop.init (~2.5-3 s) — runs on
    # one daemon thread. It must COMPLETE before the first turn opens its root
    # span and before the first LangChain run configures a callback manager;
    # turn_span() waits on ensure_init_resolved() for exactly that, keeping
    # the synchronous boot path down to the cheap decisions above.
    _start_arm_thread(endpoint)


@contextlib.contextmanager
def turn_span(*, name: str, session_id: str, turn: int) -> Generator[None, None, None]:
    """Wrap ONE graph invocation (= one agent turn) in an OTel root span.

    The trace boundary is the turn: the runloop invokes the graph once per
    turn, so this span opens when the invocation starts — including claim's
    long wait for the turn's inbound (recorded separately, see
    claim_idle_wait_span). All child spans (LangGraph nodes, Anthropic calls,
    etc.) inherit the OTel context and become children of this root, so the
    recorded mirror shows a proper span tree per turn. The session_id
    attribute groups one agent's turns into a session on the viewer; the turn
    attribute orders them within a process lifetime.

    **Placeholder root — exported at turn START, not at turn end (#1964).**
    The root span is ended immediately after creation, so the batch processor
    exports it within one flush (5 s in production, immediately under a
    SimpleSpanProcessor in tests) while the turn is still running. Residual
    window: a turn shorter than the flush cadence whose process then dies
    hard loses the whole unflushed batch (root and children together) — a
    missing trace, never a rootless one. A trace
    therefore ALWAYS has its root, even when the process is killed mid-turn
    (SIGKILL / OOM / crash): the previous behaviour ended the root only when
    the turn's work finished, so a process that died with the root still open
    left a rootless trace in Tempo (children export as they end, the root
    never does). The trade-off: the root's duration reads 0 and its end time
    is the turn start — the turn's real duration is carried by the child
    spans (the batch/LLM spans' min start .. max end), which is how
    Grafana/Tempo compute the trace span anyway. The ended root stays the
    current span in the OTel context for the whole turn (use_span,
    end_on_exit=False), so children still parent under it; the context
    detaches on exit without a second end call.

    No-op when trace_enabled=False or initialize_tracing hasn't run yet; a
    pending background init is awaited first (ensure_init_resolved) so the
    root span never opens against the unset proxy tracer.
    """
    if not settings.observability.trace_enabled:
        yield
        return
    ensure_init_resolved()
    if not _state["initialized"]:
        yield
        return
    from opentelemetry import trace as otel_trace

    tracer = otel_trace.get_tracer("ava.session")
    # Placeholder root: created, attributed and ended at turn start so the
    # trace has its root even if this process dies mid-turn. The context
    # stays attached (end_on_exit=False) so every child span of the turn
    # parents under this span; the CM exit only detaches the context and
    # never ends the span a second time (Span.end is idempotent but warns).
    root = tracer.start_span(name)
    root.set_attribute(_ATTR_NEUTRAL_SESSION_ID, session_id)
    root.set_attribute(_ATTR_TURN, turn)
    root.end()
    with otel_trace.use_span(
        root, end_on_exit=False, record_exception=False, set_status_on_exception=False
    ):
        yield


@contextlib.contextmanager
def claim_idle_wait_span() -> Generator[None, None, None]:
    """Close the claim node span at the park boundary and record the idle wait
    as an explicit `claim idle-wait` span.

    The claim node blocks in `_wait_for_batch` (Redis pub/sub wait + defensive
    SELECT recheck — up to ~30 s per round, unbounded rounds) when an idle
    agent has no inbound. The LangChain instrumentor opened `execute_task
    claim` around the whole node, so without this helper the idle park is
    drawn as one giant opaque node span (observed: 451 s and 697 s traces).
    Ending the node span here makes `execute_task claim` show only the real
    dispatch (ms), and the wait itself stays visible and attributed as a
    labeled `claim idle-wait` span (parented under the ended node span, same
    trace) instead of vanishing.

    The ended span's later counterpart — the instrumentor's `on_chain_end` —
    sees `end_time` already set and skips its own end (its `_end_span` guard),
    so no double-end warning; the node span is exported at the moment the
    wait begins. The handler's post-end `gen_ai.task.status` write is dropped
    by the SDK (one "Setting attribute on ended span." log line per park —
    information-free, kept in the log files, filtered out of the events table
    by `shared/log.py:_event_pipeline_filter`).

    No-op when trace_enabled=False or initialize_tracing hasn't run yet, and —
    defensively — when the current span is not recording or is not a LangChain
    node span (`execute_task ...`). The helper may only end the claim node's
    own span; with the instrumentor absent the wait simply stays inside
    whatever span is current (the pre-fix behavior).
    """
    if not settings.observability.trace_enabled:
        yield
        return
    ensure_init_resolved()
    if not _state["initialized"]:
        yield
        return
    from opentelemetry import trace as otel_trace

    current = otel_trace.get_current_span()
    if not current.is_recording():
        yield
        return
    # `name` is SDK-only (not on the otel API Span type, hence the cast); the
    # runtime object is the SDK span the callback handler started, and the
    # prefix check is the fail-safe that keeps us from ending the enclosing
    # workflow/root span when the node instrumentor is not attached.
    if not cast(SdkSpan, current).name.startswith(_NODE_SPAN_PREFIX):
        yield
        return
    current.end()
    tracer = otel_trace.get_tracer("ava.claim")
    with tracer.start_as_current_span(_IDLE_WAIT_SPAN_NAME):
        yield
