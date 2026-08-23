"""The unified event emitter (`shared.telemetry`) — event-shape contract.

Pins the Layer-1 contract of the event-system refactor: every `emit()` lands
one line in the JSONL mirror (the durable local copy; the Postgres `events`
copy was retired with the LGTM cutover, task #1197) with the unified event
shape (event_name, category, level, source, attributes, trace correlation),
and `trace_id`/`span_id` are captured from the active OTel span at enqueue
time — no per-callsite plumbing. The OTLP export (Loki/Prometheus) is the
same batch, covered by telemetry_otlp's tests.

The emitter pipeline is a per-process singleton; tests bind process identity
explicitly and flush before every assertion (the drain thread writes batches
asynchronously).
"""

from __future__ import annotations

import json
import socket
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from opentelemetry import trace as otel_trace
from opentelemetry.trace import NonRecordingSpan, SpanContext

from shared import observability, telemetry

_AGENT = 8901


def _mirror_rows(event_name: str, agent: int | None = None) -> list[dict[str, Any]]:
    """Every mirror line for (event_name[, agent]) — fail loud on absence.

    The mirror is day-stamped and cumulative across tests, so callers filter
    by event_name (each test uses its own event name); `_mirror_last` is for
    the cases that need the most recent row only.
    """
    from shared.paths import logs_dir

    telemetry.flush()
    day = datetime.now(UTC).strftime("%Y%m%d")
    path = logs_dir() / f"events-{day}.jsonl"
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if obj.get("event_name") == event_name and (agent is None or obj.get("agent_id") == agent):
            rows.append(obj)
    return rows


def _mirror_last(event_name: str, agent: int | None = None) -> dict[str, Any]:
    rows = _mirror_rows(event_name, agent)
    assert rows, f"expected a mirror line for {event_name}/{agent}, got none"
    return rows[-1]


@pytest.fixture(autouse=True)
def _bind_telemetry() -> None:
    """Bind a stable process identity for every test in this module."""
    telemetry.init_telemetry(process="test-proc")


# ── the unified event shape ──────────────────────────────────────────────────


def test_emit_writes_unified_event_shape() -> None:
    telemetry.emit(
        "telemetry",
        "llm_usage",
        level="info",
        agent_id=_AGENT,
        source="system",
        attributes={"in_total": 100, "model": "deepseek-v4"},
    )
    obj = _mirror_last("llm_usage", _AGENT)
    assert obj["event_name"] == "llm_usage"
    assert obj["category"] == "telemetry"
    assert obj["level"] == "info"
    assert obj["agent_id"] == _AGENT
    assert obj["machine"]  # required dimension — always filled
    assert obj["cluster"]  # home-derived dimension — always filled
    assert obj["process"] == "test-proc"
    assert obj["source"] == "system"
    assert obj["target_agent_id"] is None
    assert obj["trace_id"] is None and obj["span_id"] is None  # no OTel span active
    assert obj["attributes"]["in_total"] == 100
    assert obj["attributes"]["model"] == "deepseek-v4"


def test_audit_event_writes_unified_shape() -> None:
    telemetry.emit(
        "audit",
        "send_message",
        agent_id=_AGENT,
        source="agent:8902",
        target_agent_id=_AGENT + 1,
        attributes={"inbound_id": 42},
    )
    obj = _mirror_last("send_message", _AGENT)
    assert obj["category"] == "audit"
    assert obj["source"] == "agent:8902"
    assert obj["target_agent_id"] == _AGENT + 1
    assert obj["attributes"] == {"inbound_id": 42}


def test_telemetry_and_log_events_land_in_mirror() -> None:
    telemetry.emit("telemetry", "turn_end", agent_id=_AGENT, attributes={"ok": True})
    telemetry.emit("log", "log", agent_id=_AGENT, attributes={"msg": "hello"})
    turn_end = _mirror_last("turn_end", _AGENT)
    bare_line = _mirror_last("log", _AGENT)
    assert turn_end["category"] == "telemetry"
    assert bare_line["category"] == "log"
    assert bare_line["attributes"] == {"msg": "hello"}


# ── trace correlation ─────────────────────────────────────────────────────────


def test_trace_ids_captured_from_active_otel_span() -> None:
    """Events emitted inside an OTel span carry its trace_id/span_id — the
    turn_span() correlation contract, read from context not passed by hand."""
    span_ctx = SpanContext(
        trace_id=0x1234567890ABCDEF1234567890ABCDEF,
        span_id=0x1234567890ABCDEF,
        is_remote=False,
    )
    with otel_trace.use_span(NonRecordingSpan(span_ctx), end_on_exit=False):
        telemetry.emit("telemetry", "exec", agent_id=_AGENT, attributes={"tool": "shell"})
        telemetry.emit("audit", "label_change", agent_id=_AGENT, source="self")
    obj = _mirror_last("exec", _AGENT)
    assert obj["trace_id"] == "1234567890abcdef1234567890abcdef"
    assert obj["span_id"] == "1234567890abcdef"
    # business (audit) events get the same correlation
    obj2 = _mirror_last("label_change", _AGENT)
    assert obj2["trace_id"] == obj["trace_id"] and obj2["span_id"] == obj["span_id"]


def test_no_span_means_null_trace() -> None:
    # ensure we are OUTSIDE any span context (previous test's use_span exited)
    assert otel_trace.get_current_span().get_span_context().is_valid is False
    telemetry.emit("log", "log", agent_id=None, source="system")
    obj = _mirror_last("log")
    assert obj["trace_id"] is None and obj["span_id"] is None


# ── machine / process dimensions ──────────────────────────────────────────────


def test_machine_falls_back_to_hostname_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    """The machine dimension is required: without a configured machine_name the
    emitter falls back to the hostname so the field is never empty."""
    import shared.machine as sm

    def _raise() -> str:
        raise sm.MachineNameMissing("no machine name")

    monkeypatch.setattr(sm, "machine_name", _raise)
    assert telemetry._resolve_machine() == socket.gethostname()


def test_cluster_label_falls_back_without_raising(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    home = tmp_path / ".ava-preview"

    def fail_label(_home: Any) -> str:
        raise RuntimeError("label unavailable")

    def fallback_slug(_home: Any) -> str:
        return "ava-preview-fallback"

    monkeypatch.setattr("shared.cluster.home_label", fail_label)
    monkeypatch.setattr("shared.cluster.home_slug", fallback_slug)
    assert observability.cluster_label(home) == "ava-preview-fallback"

    monkeypatch.setattr("shared.cluster.home_slug", fail_label)
    assert observability.cluster_label(home) == ".unknown"


def test_category_for_kind_mapping() -> None:
    assert telemetry.category_for_kind("llm_usage") == "telemetry"
    assert telemetry.category_for_kind("send_message") == "audit"
    assert telemetry.category_for_kind("log") == "log"
    assert telemetry.category_for_kind("no_such_event") == "log"


# ── JSONL mirror ──────────────────────────────────────────────────────────────


def test_jsonl_mirror_holds_every_event() -> None:
    from shared.paths import logs_dir

    telemetry.emit("audit", "fork", agent_id=_AGENT, source="system")
    telemetry.flush()
    day = datetime.now(UTC).strftime("%Y%m%d")
    path = logs_dir() / f"events-{day}.jsonl"
    assert path.exists(), f"mirror file missing: {path}"
    lines = path.read_text(encoding="utf-8").splitlines()
    assert any('"event_name":"fork"' in line and '"category":"audit"' in line for line in lines)


def test_jsonl_mirror_ids_are_stable_and_match_the_id_free_body() -> None:
    """Mirror rows share Loki's id derivation, so mirror consumers can deduplicate."""
    marker = uuid4().hex
    ts = datetime(2026, 8, 23, 12, 0, tzinfo=UTC)
    for sequence in range(3):
        telemetry.emit(
            "telemetry",
            "exec",
            agent_id=_AGENT,
            attributes={"mirror_id_test": marker, "sequence": sequence},
            ts=ts,
        )

    rows = [
        row
        for row in _mirror_rows("exec", _AGENT)
        if row["attributes"].get("mirror_id_test") == marker
    ]
    assert len(rows) == 3
    assert all(isinstance(row["id"], int) for row in rows)
    assert len({row["id"] for row in rows}) == len(rows)

    first_body: str | None = None
    first_ts_ns: int | None = None
    for row in rows:
        body = {key: value for key, value in row.items() if key != "id"}
        body_str = json.dumps(body, default=str, separators=(",", ":"), ensure_ascii=False)
        ts_ns = int(datetime.fromisoformat(row["ts"]).timestamp() * 1_000_000_000)
        assert row["id"] == telemetry.event_id(body_str, ts_ns)
        if first_body is None:
            first_body = body_str
            first_ts_ns = ts_ns

    assert first_body is not None and first_ts_ns is not None
    assert telemetry.event_id(first_body, first_ts_ns) == telemetry.event_id(
        first_body, first_ts_ns
    )


def test_jsonl_mirror_true_duplicate_rows_have_equal_ids() -> None:
    marker = uuid4().hex
    ts = datetime(2026, 8, 23, 12, 0, tzinfo=UTC)
    for _ in range(2):
        telemetry.emit(
            "telemetry",
            "exec",
            agent_id=_AGENT,
            attributes={"mirror_id_duplicate_test": marker},
            ts=ts,
        )

    rows = [
        row
        for row in _mirror_rows("exec", _AGENT)
        if row["attributes"].get("mirror_id_duplicate_test") == marker
    ]
    assert len(rows) == 2
    assert rows[0]["id"] == rows[1]["id"]


# ── resilience ────────────────────────────────────────────────────────────────


def test_emit_best_effort_for_registered_names() -> None:
    """Emit is best-effort for registered events: exotic attributes, null
    agent, anything — no raise (the pipeline may be absent; that path is
    suppressed, never raised)."""
    telemetry.emit("telemetry", "sse_drop", agent_id=None, attributes={"ts": datetime.now(UTC)})
    telemetry.emit("audit", "spawn", agent_id=None, source="system")
    telemetry.flush()
    assert True


def test_emit_unregistered_name_raises() -> None:
    with pytest.raises(ValueError):
        telemetry.emit("telemetry", "no_such_event", agent_id=None, source="system")  # type: ignore[arg-type]


def test_loguru_adapter_sets_source_from_extra(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`source=` on a logger call lands in the unified source field (default
    'system')."""
    from loguru import logger as _g

    from shared.log import _postgres_sink

    sink_id = _g.add(_postgres_sink, level="INFO", enqueue=False, catch=False)
    try:
        _g.configure(extra={"agent_id": str(_AGENT)})
        _g.info("a line from the user path", event="label_change", source="user")
    finally:
        _g.remove(sink_id)
    obj = _mirror_last("label_change", _AGENT)
    assert obj["source"] == "user"
    assert obj["attributes"] == {"msg": "a line from the user path"}


def test_drain_on_exit_lands_queued_events() -> None:
    """The atexit hook must land events still queued (or in the drain thread's
    in-flight batch) — the `process_exit` event's own survival depends on it.
    Runs last-ish: it stops the shared pipeline, so a fresh one is brought up
    afterwards for the rest of the session."""
    telemetry.emit("telemetry", "process_exit", agent_id=_AGENT)
    telemetry._drain_on_exit()  # flush + stop — no explicit telemetry.flush() here
    assert _mirror_rows("process_exit", _AGENT), "process_exit must land in the mirror"
    # _drain_on_exit stopped the shared pipeline — bring a fresh one up
    telemetry._state["pipeline"] = None  # type: ignore[attr-defined]
    telemetry.init_telemetry(process="test-proc")


def test_resolve_machine_falls_back_only_on_documented_absence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    machine = telemetry._resolve_machine()  # type: ignore[attr-defined]
    assert machine  # non-empty on this host
