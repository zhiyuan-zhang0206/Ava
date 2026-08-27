"""Trace recording — OTLP/HTTP export to the local OTel Collector sidecar.

**Record** (this module): called once per process early in `agent/loop.py:main()`.
When `trace_enabled` (default on), OpenLLMetry (traceloop-sdk) auto-instruments
the Anthropic/OpenAI/Google SDKs + LangChain/LangGraph, and every span is
exported over OTLP/HTTP (protobuf wire format) to the local OTel Collector sidecar
(`AVA_TELEMETRY_OTLP_ENDPOINT`, default 127.0.0.1:4318) by
`OtlpJsonHttpSpanExporter`. The sidecar (one per machine, supervised like every
other Ava service) writes the local JSONL mirror under `$AVA_HOME/traces/` via
its file exporter. A gateway sidecar fans out to loopback Tempo; a pure runner
sidecar relays to the gateway collector's authenticated private receiver. Each
line is one standard OTLP/JSON `ExportTraceServiceRequest` (the same wire shape
any OTLP backend ingests), giving accepted batches a vendor-neutral, grep-able
local recovery copy. A gap present in the mirror can be replayed with
`ava trace ship`.

  **Metadata-only by default (trace v2, 2026-08-05)**: spans record chain
  metadata (span names, langgraph paths, checkpoint refs, agent_id, durations,
  status, trace_id) but NOT LLM content. Content stripping happens at two
  layers: `TRACELOOP_TRACE_CONTENT=false` stops the instrumentors from attaching
  prompt/completion attributes at the source (2026-08-05 incident: 31 GB/h of
  mirror, 99.89% of it `gen_ai.task.input/output`-style LLM content), and
  `OtlpJsonHttpSpanExporter` re-strips defensively before anything leaves the
  process (a future instrumentor that ignores the env var cannot leak content
  back in — stripped here, it never reaches the sidecar, the mirror or Tempo).
  Turn content is fetched on demand from the checkpoints table by trace id —
  see `shared/checkpoint.py` / the gateway trace endpoint.

- **Ship** (`cli/commands/trace.py`, `ava trace ship`): replays a time window of
  the sidecar's JSONL mirror to Tempo directly on a gateway, or through the
  gateway collector's authenticated receiver on a pure runner. This is the
  recovery path (backend down
  longer than the collector queue held, offline machines) — the live fan-out is
  the collector's job. It is resumable via a watermark, and it gates on
  `telemetry_otlp_enabled` — one kill switch for the whole OTLP surface (with
  the sidecar architecture this also stops recording, since recording IS the
  OTLP export).

turn_span() in agent/_runloop.py wraps each graph.ainvoke — one invocation =
one agent TURN — in a native OTel root span so every LLM/tool child span of
that turn nests under it and the trace exports the moment the turn ends. The
root carries the vendor-neutral `session.id` (Tempo/Grafana group one agent's
turns into a session by it) plus `ava.turn` (the per-process turn counter).

Idempotent: the _initialized guard prevents a second init. A collector miss at
startup logs once, then one daemon loop retries every five minutes until trace
recording comes up; disk-watermark auto-degrade remains a deliberate no-retry.

History: Laminar -> Langfuse 2026-06-06; Langfuse -> Tempo (LGTM) 2026-08-11;
agent-side mirror -> local collector sidecar (task #1266) 2026-08-14.
"""

from __future__ import annotations

import contextlib
import gzip
import json
import os
import re
import shutil
import threading
import time
from collections.abc import Callable, Generator, Sequence
from datetime import UTC, date, datetime, timedelta
from functools import wraps
from pathlib import Path
from typing import Any

import httpx
from opentelemetry.sdk.trace import ReadableSpan
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

# Span attribute keys.
#
# `session.id` — vendor-neutral session grouping key (emerging GenAI semantic
# convention); the consumer groups one agent's spans into a session by it.
_ATTR_NEUTRAL_SESSION_ID = "session.id"

# `ava.turn` — the per-process turn counter on each turn's root span; orders
# an agent's turns within one process lifetime (it restarts at 1 on respawn;
# cross-process ordering comes from timestamps / checkpoint refs).
_ATTR_TURN = "ava.turn"


# ── LLM content stripping (trace v2) ─────────────────────────────────────────
#
# Span attributes that carry LLM content — the 99.89% of the 2026-08-05 mirror
# blowup (31 GB/h with AVA_TRACE_ENABLED=true). Chain metadata lives in the
# `traceloop.association.properties.*` / `gen_ai.task.*` / `langgraph.*`
# attributes and never appears here; these keys are pure payload.
#
# Blacklist (exact keys) + prefix rules (gen_ai.input.* / gen_ai.output.*):
# both instrumentors we use (openllmetry anthropic/openai/langchain/google)
# emit prompts/completions under gen_ai.* per the GenAI semantic convention,
# and traceloop's langchain wrapper mirrors them as traceloop.entity.*.
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
# durations). Anything bigger than this on a single string value is treated as
# content even if an instrumentor invents a new key — bounds a future leak to
# a few KB per span instead of the megabyte-scale blobs that caused the
# incident. 16 KB is comfortably above the largest real metadata (a long
# langgraph path array) and far below the smallest real content (a prompt).
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
    trace/span ids and every `traceloop.*` / `gen_ai.task.*` / `langgraph.*`
    metadata attribute are untouched.
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
}
_init_lock = threading.Lock()

# Mirror filenames (sidecar file exporter layout since task #1266):
#   spans.jsonl                          — the ACTIVE file the collector appends to
#   spans-<ISO-timestamp>-size.jsonl     — collector-rotated backups (timberjack
#                                          1.4.5, pinned by otelcol-contrib 0.155.0,
#                                          appends the trigger reason to the
#                                          backup name: `-size` / `-time`;
#                                          `spans-2026-08-27T03-29-10.942-size.jsonl`,
#                                          stamped in UTC)
#   spans-<ISO-timestamp>.jsonl          — older collector-rotated backups
#                                          (pre-0.155.0 lumberjack, no reason suffix)
#   spans-YYYYMMDD-<pid>.jsonl           — legacy agent-side mirror (pre-#1266)
#   spans.cut-YYYYMMDD.jsonl             — manual "cut" of the active file (ops
#                                          one-off): the collector keeps appending
#                                          to the renamed inode until its next size
#                                          rotation, then a fresh spans.jsonl is born
#   <any of the above>.gz                — compressed by `_gzip_old_mirror` once the
#                                          file stops being written; consumers
#                                          (`ava trace ship`, inspect-a-trace) read
#                                          these transparently
# Old and rotated names parse their day from the name; the active file has no day
# stamp and is therefore never a retention/prune target (the collector's own
# rotation bounds it).
_MIRROR_DAY_RE = re.compile(r"^spans-(\d{8})-(\d+)\.jsonl(?:\.gz)?$")
_MIRROR_ROTATED_RE = re.compile(
    r"^spans-(\d{4})-(\d{2})-(\d{2})T(\d{2})-(\d{2})-(\d{2})\.\d+"
    r"(?:-(?:size|time))?\.jsonl(?:\.gz)?$"
)
_MIRROR_CUT_RE = re.compile(r"^spans\.cut-(\d{8})\.jsonl(?:\.gz)?$")


def _mirror_day(path: Path) -> date | None:
    """Day stamp of a mirror file from its name; None when it carries none.

    Old agent-side files stamp `spans-YYYYMMDD-<pid>.jsonl`; the collector's
    rotated backups stamp `spans-YYYY-MM-DDTHH-MM-SS.<ms>(-size|-time)?.jsonl`
    (timberjack 1.4.5 appends the trigger reason; optional `.gz` after the
    agent-side gzip pass); manual cuts stamp `spans.cut-YYYYMMDD.jsonl`; the
    ACTIVE `spans.jsonl` carries no stamp at all (never pruned, bounded by
    the collector's own rotation).
    """
    m = _MIRROR_DAY_RE.match(path.name)
    if m:
        return datetime.strptime(m.group(1), "%Y%m%d").date()  # noqa: DTZ007 — date-only
    m = _MIRROR_ROTATED_RE.match(path.name)
    if m:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    m = _MIRROR_CUT_RE.match(path.name)
    if m:
        return datetime.strptime(m.group(1), "%Y%m%d").date()  # noqa: DTZ007 — date-only
    return None


def _mirror_epoch(path: Path) -> int:
    """Sub-day order key: pid for legacy files, epoch seconds for rotated
    backups (both share the day key in `_mirror_sort_key`); 0 for the active
    file. A name that parses as neither sorts last with day +inf — an
    unrecognized file is never the deletion target."""
    m = _MIRROR_DAY_RE.match(path.name)
    if m:
        return int(m.group(2))
    m = _MIRROR_ROTATED_RE.match(path.name)
    if m:
        y, mo, d, h, mi, s = (int(m.group(i)) for i in range(1, 7))
        return int(datetime(y, mo, d, h, mi, s, tzinfo=UTC).timestamp())
    # A manual cut carries no sub-day order key; the day stamp alone orders it.
    return 0


def _prune_old_mirror(retention_days: int) -> None:
    """Delete mirror files whose day stamp is older than retention_days.

    Recording is always-on but shipping may be off, so the mirror grows
    unbounded without this. Pruned on each agent start (the only frequent
    recording entry); current-day files (the only ones being appended) are never
    in range. A window not shipped within retention_days is gone — that is the
    documented retention contract, not silent loss.
    """
    if retention_days <= 0:
        return
    cutoff = (datetime.now(UTC) - timedelta(days=retention_days)).date()
    removed = 0
    # `.jsonl.gz` included: the gzip pass renames old segments, and retention
    # must still reach them (their day stamp parses from the name).
    for path in traces_dir().glob("spans*.jsonl*"):
        day = _mirror_day(path)
        if day is not None and day < cutoff:
            path.unlink(missing_ok=True)
            removed += 1
    if removed:
        logger.info(
            "pruned old trace mirror files",
            event="trace",
            removed=removed,
            cutoff=cutoff.isoformat(),
        )


def _gzip_old_mirror(grace_seconds: int = 60) -> int:
    """gzip rotated (non-active) mirror files; return the number compressed.

    The collector's file exporter cannot compress its own rotated backups
    (fileexporter 0.155.0 forces timberjack Compression "none"; its zstd
    option applies to the ACTIVE stream and would break the grep surface),
    so this pass — running on each agent start next to the retention prune —
    compresses every mirror file except the ACTIVE `spans.jsonl`. JSONL
    compresses ~5-10x, so the recovery mirror's disk footprint drops
    accordingly. Consumers read `.jsonl.gz` transparently: `ava trace ship`
    continues from the per-file watermark keyed by the base name (a gzip pass
    never strands unshipped lines), and the inspect-a-trace mirror fetcher
    decompresses on read.

    Idempotent: already-compressed `.gz` files are skipped; a crash between
    writing the `.gz` and unlinking the original leaves both, and the next
    pass re-compresses (overwrites) and unlinks. A manual "cut" of the active
    file (the collector keeps appending to the renamed inode until its next
    size rotation) is skipped within `grace_seconds` of its last write, so a
    file still being appended is never compressed mid-write; timberjack-
    rotated backups are static by construction and only the cut edge case is
    guarded. The tail written after the rename-away is bounded loss under the
    mirror's documented contract (it still reached Tempo through the
    collector's live fan-out). A negative grace compresses regardless of
    mtime (tests).
    """
    cutoff = time.time() - grace_seconds
    compressed = 0
    for path in traces_dir().glob("spans*.jsonl*"):
        if path.name == "spans.jsonl" or path.name.endswith(".gz"):
            continue
        try:
            if path.stat().st_mtime > cutoff:
                continue
        except OSError:
            continue
        gz_path = Path(f"{path}.gz")
        try:
            # mtime=0 keeps the compressed bytes deterministic (tests, dedup).
            with (
                path.open("rb") as src,
                gzip.GzipFile(gz_path, mode="wb", compresslevel=6, mtime=0) as dst,
            ):
                shutil.copyfileobj(src, dst, length=1024 * 1024)
            path.unlink(missing_ok=True)
        except OSError:
            gz_path.unlink(missing_ok=True)
            continue
        compressed += 1
    if compressed:
        logger.info(
            "gzipped old trace mirror files",
            event="trace",
            compressed=compressed,
            grace_seconds=grace_seconds,
        )
    return compressed


def _mirror_sort_key(p: Path) -> tuple[int, int]:
    """Oldest-first key for a mirror file (day, sub-day order).

    Day comes from the name (`_mirror_day`); sub-day is the numeric pid for
    legacy files (string order would prune `...-1000` before `...-999`,
    deleting a newer file) or the epoch seconds of a rotated backup. The
    ACTIVE `spans.jsonl` (no day stamp) sorts last — the cap prune never
    deletes the file being written unless the cap is absurdly small (same
    contract as retention: bounded disk, documented loss, never silent). No
    stat — the file may vanish mid-parse.
    """
    day = _mirror_day(p)
    if day is not None:
        return (day.toordinal(), _mirror_epoch(p))
    return (2**31, 0)


def _mirror_size(p: Path) -> int:
    """Size of a mirror file, 0 if it vanished between glob and stat.

    The traces dir is shared by every agent process on the host; a peer's
    prune can unlink a file this process already globbed, and a bare stat
    would then raise FileNotFoundError — out of the agent boot path, which
    calls `_enforce_dir_cap` with no try/except (a peer pruning at the same
    moment could kill an agent start).
    """
    try:
        return p.stat().st_size
    except OSError:
        return 0


def _enforce_dir_cap(max_mb: int) -> int:
    """Delete oldest mirror files until the traces directory fits under `max_mb`.

    Iterates oldest-first by day stamp + numeric pid (`_mirror_sort_key`).
    Never deletes the file currently being written (the newest, by key) unless
    the cap is absurdly small — same contract as retention: bounded disk,
    documented loss, never silent. Returns the number of files deleted. No-op
    when max_mb <= 0.
    """
    if max_mb <= 0:
        return 0
    cap_bytes = max_mb * 1024 * 1024
    files = sorted(traces_dir().glob("spans*.jsonl*"), key=_mirror_sort_key)
    total = sum(_mirror_size(p) for p in files)
    removed = 0
    for p in files:
        if total <= cap_bytes:
            break
        total -= _mirror_size(p)
        p.unlink(missing_ok=True)
        removed += 1
    if removed:
        logger.info(
            "trace mirror over size cap — removed oldest files",
            event="trace",
            removed=removed,
            cap_mb=max_mb,
        )
    return removed


def _disk_usage() -> tuple[float, int] | None:
    """(usage fraction, free bytes) of the traces dir's data disk.

    The mirror is written to $AVA_HOME/traces, so the data disk is the one that
    matters. A stat failure (exotic filesystem) returns None — the guard must
    never be the reason recording is off.
    """
    try:
        usage = shutil.disk_usage(traces_dir())
    except OSError:
        return None
    return usage.used / usage.total, usage.free


def _disk_watermark_exceeded(watermark: float) -> bool:
    """True when the data disk's usage fraction exceeds the watermark.

    `watermark >= 1.0` disables the guard (the fraction can never exceed 1.0).
    """
    if watermark >= 1.0:
        return False
    usage = _disk_usage()
    return usage is not None and usage[0] > watermark


class OtlpJsonHttpSpanExporter(SpanExporter):
    """OTLP/HTTP span exporter to the local OTel Collector (protobuf wire).

    Each `export()` batch is encoded to the OTLP dict, content-stripped
    (defensive layer 2 — stripped here, content never leaves the process),
    re-serialized to the OTLP `ExportTraceServiceRequest` protobuf and POSTed
    to `{endpoint}/v1/traces` with `Content-Type: application/x-protobuf` —
    the same wire path `ava trace ship` uses. Protobuf, not OTLP/JSON, on the
    wire: the collector's JSON receiver rejects the SDK's padded-base64 ids
    (verified 2026-08-14 against otelcol-contrib 0.155.0), and protobuf is
    the canonical OTLP/HTTP encoding. The endpoint is the LOCAL sidecar
    (``AVA_TELEMETRY_OTLP_ENDPOINT``, default 127.0.0.1:4318); agents never
    dial a backend directly.

    The sidecar is supervised (watchdog, 60s), so a failed POST means a
    restart in progress or a broken install: retry briefly, then return
    FAILURE and let the SDK's batch processor drop the batch — bounded loss,
    reported. The mirror is the collector's job now; this exporter's only
    duties are stripping (defensive layer 2, before anything leaves the
    process) and the POST itself.
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
    """Retry the collector preflight on one daemon thread until init succeeds."""
    while not _state["initialized"]:
        time.sleep(COLLECTOR_RETRY_INTERVAL_S)
        if _state["initialized"]:
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

    No-op when settings.observability.trace_enabled=False. When on, builds an
    OtlpJsonHttpSpanExporter (pointed at the local OTel Collector sidecar) and
    hands it to Traceloop.init as the sole exporter — the explicit
    `instruments=` set covers Anthropic + OpenAI + LangChain + Google GenAI so
    all LLM call paths produce spans that nest under the per-turn root span
    (see turn_span()). LangGraph is instrumented through the LangChain
    instrumentor (callback handler), not a separate one.

    Recording is one OTLP/HTTP hop to the LOCAL sidecar; the sidecar writes the
    JSONL mirror and either fans out locally or relays to the gateway. Its
    queues decouple ordinary backend outages, while prolonged pressure can
    still return backpressure before the mirror. If the sidecar itself is not
    answering at init, recording stays off temporarily: the episode is logged
    once and one daemon loop re-checks every five minutes until init succeeds.

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
    if _state["initialized"]:
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

    # Content stripping, layer 1 (the source): the openllmetry instrumentors
    # check TRACELOOP_TRACE_CONTENT on every prompt/completion they would
    # attach, so setting it false before init means LLM content never becomes
    # a span attribute in the first place. Set unconditionally when
    # strip_content is on — an operator-set true would silently re-open the
    # 31 GB/h hole. Layer 2 (the exporter) stays armed regardless.
    if settings.observability.trace_strip_content:
        os.environ["TRACELOOP_TRACE_CONTENT"] = "false"

    from traceloop.sdk import Traceloop
    from traceloop.sdk.instruments import Instruments

    # Mirror the LLM call paths Ava actually uses. LangGraph rides on the
    # LANGCHAIN instrumentor (its callback handler nests node spans), so there
    # is no separate LANGGRAPH instrument. LiteLLM, when used, surfaces through
    # the provider instrumentors (Anthropic/OpenAI) at the SDK layer.
    instruments = {
        Instruments.ANTHROPIC,
        Instruments.OPENAI,
        Instruments.LANGCHAIN,
        Instruments.GOOGLE_GENERATIVEAI,
    }

    # `exporter=` makes OtlpJsonHttpSpanExporter the sole span exporter, pointed
    # at the LOCAL OTel Collector sidecar (no api_endpoint/api_key, so the SDK
    # never dials a remote backend itself). `telemetry_enabled=False` stops
    # traceloop-sdk's own anonymous usage telemetry from phoning home to
    # api.traceloop.com.
    # traceloop-sdk's inlined init() signature types resource_attributes as
    # dict[Unknown, Unknown]; the call below passes only fully-typed kwargs.
    Traceloop.init(  # pyright: ignore[reportUnknownMemberType]
        app_name="ava",
        exporter=OtlpJsonHttpSpanExporter(endpoint=endpoint),
        instruments=instruments,
        disable_batch=False,
        telemetry_enabled=False,
        resource_attributes={"cluster": cluster_label()},
    )

    logger.info(
        "trace recording enabled",
        event="trace",
        endpoint=endpoint,
        mirror_dir=str(traces_dir()),
    )
    _state["collector_offline_reported"] = False
    _state["initialized"] = True


@contextlib.contextmanager
def turn_span(*, name: str, session_id: str, turn: int) -> Generator[None, None, None]:
    """Wrap ONE graph invocation (= one agent turn) in an OTel root span.

    The trace boundary is the turn: the runloop invokes the graph once per
    turn, so this span opens when the invocation starts — including claim's
    long wait for the turn's inbound — and closes (and exports) when the
    turn's work is done. All child spans (LangGraph nodes, Anthropic calls,
    etc.) inherit the OTel context and become children of this root, so the
    recorded mirror shows a proper span tree per turn. The session_id
    attribute groups one agent's turns into a session on the viewer; the turn
    attribute orders them within a process lifetime.

    No-op when trace_enabled=False or initialize_tracing hasn't run yet.
    """
    if not settings.observability.trace_enabled or not _state["initialized"]:
        yield
        return
    from opentelemetry import trace as otel_trace

    tracer = otel_trace.get_tracer("ava.session")
    with tracer.start_as_current_span(name) as span:
        span.set_attribute(_ATTR_NEUTRAL_SESSION_ID, session_id)
        span.set_attribute(_ATTR_TURN, turn)
        yield
