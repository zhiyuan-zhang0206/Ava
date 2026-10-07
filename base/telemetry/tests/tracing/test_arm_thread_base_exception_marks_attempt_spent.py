"""Tracing cases: arm thread base exception marks attempt spent."""

from __future__ import annotations

import threading
from pathlib import Path

import httpx
import pytest

from base.telemetry import tracing as tracing_mod
from base.telemetry.tests.test_tracing import (
    _assert_root_once_with_child_parented_under_it,
    _assert_turn_root_exported_at_start,
    _capture_otlp_json_posts,
    _FakeSpan,
    _FakeTracer,
    _NodeFakeSpan,
    _spans_received,
    _under_watermark,
    _wait_init_resolved,
)
from base.telemetry.tests.test_tracing import (
    _collector_up as _collector_up,
)
from base.telemetry.tests.test_tracing import (
    _production_process_by_default as _production_process_by_default,
)
from base.telemetry.tests.test_tracing import (
    _reset_init_flag as _reset_init_flag,
)
from base.telemetry.tracing import (
    OtlpJsonHttpSpanExporter,
    claim_idle_wait_span,
    initialize_tracing,
    turn_span,
)


def test_arm_thread_base_exception_marks_attempt_spent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The one-attempt contract holds for ANY arm-thread death: a
    BaseException escape (SystemExit/GeneratorExit from inside the SDK — not
    expected, but not impossible) must still set arm_failed, so a later
    initialize_tracing() cannot re-spawn the arm thread past the dead
    is_alive() guard (the QA #1060 corner: with `except Exception` only, a
    dead arm thread carrying no flag left one-attempt-per-process bypassable).
    """
    monkeypatch.setattr("base.config.settings.observability.trace_enabled", True)
    monkeypatch.setattr("base.telemetry.trace_mirror.traces_dir", lambda: tmp_path)
    _under_watermark(monkeypatch)
    calls: list[int] = []

    def _exit_sdk(**_kw: object) -> None:
        calls.append(1)
        raise SystemExit("sdk guard exit")

    monkeypatch.setattr("traceloop.sdk.Traceloop.init", _exit_sdk)

    initialize_tracing()
    _wait_init_resolved()

    assert tracing_mod._state["arm_failed"] is True
    assert tracing_mod._state["initialized"] is False

    initialize_tracing()  # must be a no-op: the attempt was spent
    assert calls == [1]


def test_timeout_then_late_arm_recording_comes_up_midlife(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Timeout -> late-completion contract: the bounded wait gives up and the
    first turn proceeds WITHOUT recording, but a slow (not hung) arm that
    finishes afterwards still brings recording up mid-life — later turns get
    spans; only the first turn's spans are lost (the documented price).
    Also: a later ensure call skips the wait (already remembered), so the
    timeout does not block the late-armed turn from opening its span."""
    monkeypatch.setattr("base.config.settings.observability.trace_enabled", True)
    monkeypatch.setattr("base.telemetry.trace_mirror.traces_dir", lambda: tmp_path)
    _under_watermark(monkeypatch)
    monkeypatch.setattr(tracing_mod, "_INIT_RESOLVED_TIMEOUT_S", 0.05)

    entered = threading.Event()
    release = threading.Event()

    def _slow_init(**_kw: object) -> None:
        entered.set()
        release.wait(timeout=10.0)

    from base.telemetry.tracing import ensure_init_resolved

    monkeypatch.setattr("traceloop.sdk.Traceloop.init", _slow_init)
    warnings: list[tuple[tuple[object, ...], dict[str, object]]] = []

    def _capture_warning(*a: object, **kw: object) -> None:
        warnings.append((a, kw))

    monkeypatch.setattr("base.telemetry.tracing.logger.warning", _capture_warning)

    initialize_tracing()
    assert entered.wait(timeout=5.0), "arm thread must reach Traceloop.init"

    # The arm is still in flight: the bounded wait gives up (one warning),
    # recording stays off for now...
    ensure_init_resolved()
    assert len(warnings) == 1
    assert warnings[0][1]["action"] == "init_resolved_timeout"
    assert tracing_mod._state["timeout_reported"] is True
    assert tracing_mod._state["initialized"] is False

    # ...the arm completes late: recording comes up mid-life...
    release.set()
    _wait_init_resolved()
    assert tracing_mod._state["initialized"] is True

    # ...and later calls skip the wait entirely, so the span opens for real.
    ensure_init_resolved()
    assert len(warnings) == 1
    entered_spans: list[str] = []
    with turn_span(name="t", session_id="s", turn=1):
        entered_spans.append("open")
    assert entered_spans == ["open"]


def test_ensure_init_resolved_instant_without_arming(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """No wait when tracing was declined or never requested — the event is
    only consulted after an arm thread was actually spawned."""
    from base.telemetry.tracing import ensure_init_resolved

    monkeypatch.setattr("base.config.settings.observability.trace_enabled", True)
    monkeypatch.setattr("base.telemetry.trace_mirror.traces_dir", lambda: tmp_path)
    _under_watermark(monkeypatch)
    monkeypatch.setattr(
        "base.telemetry.tracing.endpoint_reachable",
        lambda _e: False,  # pyright: ignore[reportUnknownArgumentType]
    )
    # Skip the daemon collector retry loop: this test asserts the instant
    # no-wait contract, not the loop (that is the retry-loop test above).
    monkeypatch.setattr("base.telemetry.tracing._start_collector_retry", lambda: None)

    initialize_tracing()  # declined: collector unreachable, no arm thread
    ensure_init_resolved()  # must return at once, not hang on an unset event

    assert tracing_mod._state["arm_thread"] is None
    assert tracing_mod._state["initialized"] is False


def test_otlp_exporter_posts_protobuf(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Each export() batch becomes one OTLP ExportTraceServiceRequest POSTed
    to <endpoint>/v1/traces with Content-Type application/x-protobuf (the wire
    format the collector's OTLP receiver accepts — its JSON receiver rejects
    the SDK's padded-base64 ids); the body parses back to the same spans."""
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor

    monkeypatch.setattr("base.config.settings.observability.trace_strip_content", True)
    posts = _capture_otlp_json_posts(monkeypatch)

    exporter = OtlpJsonHttpSpanExporter(endpoint="http://127.0.0.1:4318")
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer("ava.session")
    with tracer.start_as_current_span("ava-agent-7") as root:
        root.set_attribute("session.id", "7")
        with tracer.start_as_current_span("child"):
            pass
    provider.shutdown()

    assert len(posts) >= 1  # pyright: ignore[reportUnknownArgumentType]  # at least one export batch
    url, _body, headers = posts[0]
    assert url == "http://127.0.0.1:4318/v1/traces"
    assert headers["Content-Type"] == "application/x-protobuf"

    # The body is the OTLP ExportTraceServiceRequest protobuf; it must parse
    # back to exactly the recorded spans (what Tempo ingests).
    from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest

    spans = 0
    for _, raw, _h in posts:
        req = ExportTraceServiceRequest()
        req.ParseFromString(raw)
        assert req.SerializeToString()
        spans += sum(len(ss.spans) for rs in req.resource_spans for ss in rs.scope_spans)
    assert spans == 2


def test_otlp_exporter_timeout_is_bounded_and_returns_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A collector timeout costs one bounded POST and never escapes the exporter."""
    timeouts: list[float] = []

    def _timeout(_url: str, *, content: bytes, headers: dict[str, str], timeout: float):
        del content, headers
        timeouts.append(timeout)
        raise httpx.ReadTimeout("collector stalled")

    monkeypatch.setattr("httpx.post", _timeout)
    exporter = OtlpJsonHttpSpanExporter(endpoint="http://127.0.0.1:4318")

    assert exporter.export([]) is tracing_mod.SpanExportResult.FAILURE
    assert timeouts == [tracing_mod._TRACE_EXPORT_TIMEOUT_S]
    assert 0 < tracing_mod._TRACE_EXPORT_TIMEOUT_S <= 5.0


def test_otlp_exporter_circuit_drops_during_cooldown_then_recovers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Consecutive failures open the circuit; one post-cooldown probe closes it."""
    clock = {"now": 100.0}
    posts = 0

    class _Resp:
        def raise_for_status(self) -> None:
            return None

    def _post(_url: str, *, content: bytes, headers: dict[str, str], timeout: float):
        nonlocal posts
        del content, headers, timeout
        posts += 1
        if posts <= tracing_mod._TRACE_EXPORT_FAILURE_THRESHOLD:
            raise httpx.ConnectError("collector unavailable")
        return _Resp()

    monkeypatch.setattr(tracing_mod.time, "monotonic", lambda: clock["now"])
    monkeypatch.setattr("httpx.post", _post)
    exporter = OtlpJsonHttpSpanExporter(endpoint="http://127.0.0.1:4318")

    for _ in range(tracing_mod._TRACE_EXPORT_FAILURE_THRESHOLD):
        assert exporter.export([]) is tracing_mod.SpanExportResult.FAILURE
    assert posts == tracing_mod._TRACE_EXPORT_FAILURE_THRESHOLD

    assert exporter.export([]) is tracing_mod.SpanExportResult.FAILURE
    assert posts == tracing_mod._TRACE_EXPORT_FAILURE_THRESHOLD
    assert exporter._dropped_batches == 1

    clock["now"] += tracing_mod._TRACE_EXPORT_COOLDOWN_S
    assert exporter.export([]) is tracing_mod.SpanExportResult.SUCCESS
    assert posts == tracing_mod._TRACE_EXPORT_FAILURE_THRESHOLD + 1
    assert exporter._consecutive_failures == 0

    assert exporter.export([]) is tracing_mod.SpanExportResult.SUCCESS
    assert posts == tracing_mod._TRACE_EXPORT_FAILURE_THRESHOLD + 2


def test_turn_span_exports_root_at_start_not_at_end(monkeypatch: pytest.MonkeyPatch):
    """The turn root is a PLACEHOLDER: ended (and exported) at turn START, so a
    trace always has its root even when the process dies mid-turn.

    Two export-timing assertions:
    1. while the turn is still running (inside `turn_span`), the exporter has
       ALREADY received the root span (carrying session.id + ava.turn);
    2. exiting the turn does NOT export the root again — the span is ended
       once, at turn start (use_span(end_on_exit=False) detaches the context
       without a second end).

    Plus the structural contract: a child span created inside the turn parents
    under the already-ended root (same trace_id, parent_span_id == root id).
    """
    from opentelemetry import trace as otel_trace
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor

    monkeypatch.setattr("base.config.settings.observability.trace_enabled", True)
    tracing_mod._state["initialized"] = True
    posts = _capture_otlp_json_posts(monkeypatch)
    exporter = OtlpJsonHttpSpanExporter(endpoint="http://127.0.0.1:4318")
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    previous = otel_trace.get_tracer_provider()
    otel_trace.set_tracer_provider(provider)
    try:
        with turn_span(name="ava-agent-7", session_id="7", turn=3):
            # Assertion 1: the root is exported BEFORE the turn ends.
            root = _assert_turn_root_exported_at_start(_spans_received(posts))

            # A child created inside the turn must parent under the ended root.
            with otel_trace.get_tracer("ava.session").start_as_current_span("child"):
                pass

        # Assertion 2: exiting the turn does not re-export the root (and the
        # child arrived, parented under the root).
        _assert_root_once_with_child_parented_under_it(_spans_received(posts), root)
    finally:
        otel_trace.set_tracer_provider(previous)


def test_prune_old_mirror_removes_stale_keeps_recent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """_prune_old_mirror deletes files older than retention_days — legacy
    `spans-YYYYMMDD-<pid>.jsonl` AND the collector's rotated
    `spans-<ISO>.jsonl` — keeps recent ones, and never touches the unstamped
    ACTIVE `spans.jsonl` or non-mirror files."""
    from base.telemetry.tracing import _prune_old_mirror

    monkeypatch.setattr("base.telemetry.trace_mirror.traces_dir", lambda: tmp_path)
    old = tmp_path / "spans-20200101-1.jsonl"  # well before any cutoff
    old_rotated = tmp_path / "spans-2020-01-01T00-00-00.000.jsonl"  # same, rotated name
    recent = tmp_path / "spans-20990101-1.jsonl"  # well after any cutoff
    active = tmp_path / "spans.jsonl"  # the collector's active file: never pruned
    other = tmp_path / ".ship-watermark.json"  # not a mirror file
    for p in (old, old_rotated, recent, active, other):
        p.write_text("{}\n", encoding="utf-8")

    _prune_old_mirror(retention_days=14)

    assert not old.exists()
    assert not old_rotated.exists()
    assert recent.exists()
    assert active.exists()
    assert other.exists()


def test_mirror_day_parses_all_collector_name_shapes(tmp_path: Path) -> None:
    """`mirror_day` parses every mirror filename shape in the wild: legacy
    pid-dated files, collector-rotated backups WITH the timberjack trigger
    suffix (`-size` / `-time`), older unsuffixed rotated backups, manual
    `spans.cut-*` orphans, and the `.gz` variants of each — while the
    unstamped ACTIVE `spans.jsonl` stays None (never a prune target)."""
    from datetime import date

    from base.telemetry.trace_mirror import mirror_day

    cases = {
        "spans-20200101-1.jsonl": date(2020, 1, 1),
        "spans-20200101-1.jsonl.gz": date(2020, 1, 1),
        "spans-2026-08-13T23-28-01.123.jsonl": date(2026, 8, 13),
        "spans-2026-08-27T03-29-10.942-size.jsonl": date(2026, 8, 27),
        "spans-2026-08-27T03-29-10.942-time.jsonl": date(2026, 8, 27),
        "spans-2026-08-27T03-29-10.942-size.jsonl.gz": date(2026, 8, 27),
        "spans.cut-20260827.jsonl": date(2026, 8, 27),
        "spans.cut-20260827.jsonl.gz": date(2026, 8, 27),
        "spans.jsonl": None,
        "spans.jsonl.gz": None,
        ".ship-watermark.json": None,
    }
    for name, expected in cases.items():
        p = tmp_path / name
        p.touch()
        assert mirror_day(p) == expected, name


def test_mirror_sort_key_orders_suffixed_rotated_and_cut_files(
    tmp_path: Path,
) -> None:
    """The cap-prune order key handles timberjack-suffixed backups and manual
    cuts (day from the name, sub-day epoch from the timestamp), so a cap prune
    deletes them oldest-first instead of treating them like the active file."""
    from base.telemetry.trace_mirror import mirror_sort_key

    names = [
        "spans-2026-08-01T00-00-00.000-size.jsonl",
        "spans-2026-08-01T01-00-00.000-time.jsonl",
        "spans.cut-20260802.jsonl",
        "spans.jsonl",
    ]
    keys = [mirror_sort_key(tmp_path / n) for n in names]
    # Both 08-01 segments before the 08-02 cut, all before the active file.
    assert keys == sorted(keys)


def test_prune_old_mirror_removes_stale_suffixed_and_gz(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """_prune_old_mirror deletes stale files regardless of the rotation
    naming era — timberjack-suffixed (`-size`), manual cuts, and gzipped
    segments — and keeps the ACTIVE `spans.jsonl` untouched."""
    from base.telemetry.tracing import _prune_old_mirror

    monkeypatch.setattr("base.telemetry.trace_mirror.traces_dir", lambda: tmp_path)
    old_suffixed = tmp_path / "spans-2020-01-01T00-00-00.000-size.jsonl"
    old_gz = tmp_path / "spans-2020-01-01T01-00-00.000-time.jsonl.gz"
    old_cut = tmp_path / "spans.cut-20200101.jsonl"
    recent = tmp_path / "spans-2099-01-01T00-00-00.000-size.jsonl"
    active = tmp_path / "spans.jsonl"
    for p in (old_suffixed, old_gz, old_cut, recent, active):
        p.write_text("{}\n", encoding="utf-8")

    _prune_old_mirror(retention_days=14)

    assert not old_suffixed.exists()
    assert not old_gz.exists()
    assert not old_cut.exists()
    assert recent.exists()
    assert active.exists()


def test_enforce_dir_cap_counts_suffixed_and_gz_keeps_active(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """The cap prune counts suffixed rotated and gzipped files toward the
    directory size and deletes oldest-first, and never deletes the ACTIVE
    `spans.jsonl` even when every other file carries an unrecognized-era
    name (the pre-fix bug: suffixed backups sorted with the active file and
    could be deleted in either order)."""
    from base.telemetry.tracing import _enforce_dir_cap

    monkeypatch.setattr("base.telemetry.trace_mirror.traces_dir", lambda: tmp_path)
    oldest = tmp_path / "spans-2026-01-01T00-00-00.000-size.jsonl"
    newest = tmp_path / "spans-2026-01-02T00-00-00.000-time.jsonl.gz"
    active = tmp_path / "spans.jsonl"
    for p in (oldest, newest, active):
        p.write_bytes(b"x" * (1024 * 1024))

    removed = _enforce_dir_cap(max_mb=2)
    assert removed == 1
    remaining = sorted(p.name for p in tmp_path.glob("spans*.jsonl*"))
    assert remaining == ["spans-2026-01-02T00-00-00.000-time.jsonl.gz", "spans.jsonl"]


def test_gzip_old_mirror_compresses_rotated_keeps_active(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """_gzip_old_mirror compresses every non-active mirror file (rotated,
    suffixed, cut, legacy) to `.jsonl.gz` with lossless content, leaves the
    ACTIVE `spans.jsonl` alone, and is idempotent on re-run."""
    import gzip as gz

    from base.telemetry.tracing import _gzip_old_mirror

    monkeypatch.setattr("base.telemetry.trace_mirror.traces_dir", lambda: tmp_path)
    rotated = tmp_path / "spans-2026-01-01T00-00-00.000-size.jsonl"
    cut = tmp_path / "spans.cut-20260827.jsonl"
    active = tmp_path / "spans.jsonl"
    rotated.write_text('{"a":1}\n{"a":2}\n', encoding="utf-8")
    cut.write_text('{"b":1}\n', encoding="utf-8")
    active.write_text('{"c":1}\n', encoding="utf-8")

    assert _gzip_old_mirror(grace_seconds=-1) == 2
    assert rotated.exists() is False
    assert cut.exists() is False
    gz_rotated = tmp_path / "spans-2026-01-01T00-00-00.000-size.jsonl.gz"
    gz_cut = tmp_path / "spans.cut-20260827.jsonl.gz"
    assert gz_rotated.exists()
    assert gz_cut.exists()
    assert active.exists()  # never compressed
    assert not (tmp_path / "spans.jsonl.gz").exists()
    with gz.open(gz_rotated, "rt", encoding="utf-8") as fh:
        assert fh.read() == '{"a":1}\n{"a":2}\n'
    with gz.open(gz_cut, "rt", encoding="utf-8") as fh:
        assert fh.read() == '{"b":1}\n'
    # Idempotent: already-compressed files are skipped.
    assert _gzip_old_mirror(grace_seconds=-1) == 0


def test_gzip_old_mirror_skips_recently_written_files(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """A file written within the grace window is skipped (a freshly cut
    active file may still be appended by the collector until its next size
    rotation); the next pass with no grace compresses it."""
    import time

    from base.telemetry.tracing import _gzip_old_mirror

    monkeypatch.setattr("base.telemetry.trace_mirror.traces_dir", lambda: tmp_path)
    fresh = tmp_path / "spans-2026-01-01T00-00-00.000-size.jsonl"
    fresh.write_text("x\n", encoding="utf-8")
    # Touch mtime to "now" (write_text already did; keep explicit for clarity).
    now = time.time()
    import os

    os.utime(fresh, (now, now))

    assert _gzip_old_mirror(grace_seconds=3600) == 0
    assert fresh.exists()
    assert not (tmp_path / "spans-2026-01-01T00-00-00.000-size.jsonl.gz").exists()

    assert _gzip_old_mirror(grace_seconds=-1) == 1
    assert not fresh.exists()


def test_prune_old_mirror_disabled_when_nonpositive(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """retention_days <= 0 disables pruning entirely."""
    from base.telemetry.tracing import _prune_old_mirror

    monkeypatch.setattr("base.telemetry.trace_mirror.traces_dir", lambda: tmp_path)
    old = tmp_path / "spans-20200101-1.jsonl"
    old.write_text("{}\n", encoding="utf-8")
    _prune_old_mirror(retention_days=0)
    assert old.exists()


def test_turn_span_noop_when_disabled(monkeypatch: pytest.MonkeyPatch):
    """When trace_enabled=False, turn_span is a pass-through — does not open a span."""
    monkeypatch.setattr("base.config.settings.observability.trace_enabled", False)

    def _explode(*_a, **_kw):
        raise AssertionError("get_tracer should not be called when disabled")

    monkeypatch.setattr("opentelemetry.trace.get_tracer", _explode)  # pyright: ignore[reportUnknownArgumentType]

    with turn_span(name="root", session_id="agent-42", turn=1):
        pass


def test_turn_span_noop_when_initialize_skipped(monkeypatch: pytest.MonkeyPatch):
    """Even with trace_enabled=True, if initialize_tracing hasn't run yet,
    turn_span stays no-op — otherwise it opens a span against an
    uninitialized provider."""
    monkeypatch.setattr("base.config.settings.observability.trace_enabled", True)
    assert tracing_mod._state["initialized"] is False

    def _explode(*_a, **_kw):
        raise AssertionError("must not open a span when uninitialized")

    monkeypatch.setattr("opentelemetry.trace.get_tracer", _explode)  # pyright: ignore[reportUnknownArgumentType]

    with turn_span(name="root", session_id="agent-42", turn=1):
        pass


def test_turn_span_opens_root_with_session_id(monkeypatch: pytest.MonkeyPatch):
    """When enabled and initialized, turn_span opens an OTel root span with
    the given name and stamps the session and turn attributes."""
    monkeypatch.setattr("base.config.settings.observability.trace_enabled", True)
    tracing_mod._state["initialized"] = True

    span = _FakeSpan()
    tracer = _FakeTracer(span)
    monkeypatch.setattr("opentelemetry.trace.get_tracer", lambda _name: tracer)  # pyright: ignore[reportUnknownArgumentType]

    with turn_span(name="ava-agent-42", session_id="42", turn=3):
        # The placeholder root is ended (exported) at turn START, while the
        # turn is still running (#1964) — the trace has its root even when
        # the process dies mid-turn.
        assert span.ended is True

    assert tracer.opened == ["ava-agent-42"]
    assert span.attributes == {
        "session.id": "42",
        "ava.turn": 3,
    }


def test_claim_idle_wait_span_noop_when_disabled(monkeypatch: pytest.MonkeyPatch):
    """trace_enabled=False: pass-through — no OTel call at all."""
    monkeypatch.setattr("base.config.settings.observability.trace_enabled", False)

    def _explode(*_a, **_kw):
        raise AssertionError("OTel must not be touched when tracing is disabled")

    monkeypatch.setattr("opentelemetry.trace.get_current_span", _explode)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr("opentelemetry.trace.get_tracer", _explode)  # pyright: ignore[reportUnknownArgumentType]

    with claim_idle_wait_span():
        pass


def test_claim_idle_wait_span_noop_when_initialize_skipped(
    monkeypatch: pytest.MonkeyPatch,
):
    """trace_enabled=True but initialize_tracing never ran: no-op, same as
    turn_span — a span opened against the unset proxy tracer is silently lost."""
    monkeypatch.setattr("base.config.settings.observability.trace_enabled", True)
    assert tracing_mod._state["initialized"] is False

    def _explode(*_a, **_kw):
        raise AssertionError("must not open a span when uninitialized")

    monkeypatch.setattr("opentelemetry.trace.get_tracer", _explode)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr("opentelemetry.trace.get_current_span", _explode)  # pyright: ignore[reportUnknownArgumentType]

    with claim_idle_wait_span():
        pass


def test_claim_idle_wait_span_ends_node_span_and_opens_idle_wait(
    monkeypatch: pytest.MonkeyPatch,
):
    """Enabled + initialized + a recording `execute_task claim` span current:
    the node span is ended at the park boundary (so the claim span shows only
    the real dispatch) and an explicit `claim idle-wait` span is opened for
    the wait."""
    monkeypatch.setattr("base.config.settings.observability.trace_enabled", True)
    tracing_mod._state["initialized"] = True

    node_span = _NodeFakeSpan("execute_task claim")
    monkeypatch.setattr("opentelemetry.trace.get_current_span", lambda: node_span)
    tracer = _FakeTracer(_FakeSpan())
    monkeypatch.setattr("opentelemetry.trace.get_tracer", lambda _name: tracer)  # pyright: ignore[reportUnknownArgumentType]

    with claim_idle_wait_span():
        pass

    assert node_span.ended is True
    assert tracer.opened == ["claim idle-wait"]


def test_claim_idle_wait_span_never_ends_non_node_span(
    monkeypatch: pytest.MonkeyPatch,
):
    """Fail-safe: a current span that is NOT a LangChain node span (the
    enclosing turn root — the instrumentor not attached) is never ended:
    ending it would truncate the whole turn trace. The wait then stays inside
    the current span (pre-fix behavior)."""
    monkeypatch.setattr("base.config.settings.observability.trace_enabled", True)
    tracing_mod._state["initialized"] = True

    root_span = _NodeFakeSpan("ava-agent-42")
    monkeypatch.setattr("opentelemetry.trace.get_current_span", lambda: root_span)
    tracer = _FakeTracer(_FakeSpan())
    monkeypatch.setattr("opentelemetry.trace.get_tracer", lambda _name: tracer)  # pyright: ignore[reportUnknownArgumentType]

    with claim_idle_wait_span():
        pass

    assert root_span.ended is False
    assert tracer.opened == []


def test_claim_idle_wait_span_skips_non_recording_span(
    monkeypatch: pytest.MonkeyPatch,
):
    """A non-recording current span (sampler dropped it / no real span open)
    is not ended and no idle-wait span is opened — the helper only acts on a
    real recording node span."""
    monkeypatch.setattr("base.config.settings.observability.trace_enabled", True)
    tracing_mod._state["initialized"] = True

    node_span = _NodeFakeSpan("execute_task claim", recording=False)
    monkeypatch.setattr("opentelemetry.trace.get_current_span", lambda: node_span)
    tracer = _FakeTracer(_FakeSpan())
    monkeypatch.setattr("opentelemetry.trace.get_tracer", lambda _name: tracer)  # pyright: ignore[reportUnknownArgumentType]

    with claim_idle_wait_span():
        pass

    assert node_span.ended is False
    assert tracer.opened == []
