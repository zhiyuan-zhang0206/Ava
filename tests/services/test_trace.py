"""Tests for shared.trace — local OTLP-JSON span recording (no network)."""

from __future__ import annotations

import os
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

from shared import trace as trace_mod
from shared.trace import OtlpJsonHttpSpanExporter, initialize_tracing, turn_span


@pytest.fixture(autouse=True)
def _reset_init_flag():
    """Reset trace-init and retry-loop state between tests."""
    trace_mod._state.clear()
    trace_mod._state.update(
        initialized=False,
        collector_offline_reported=False,
        retry_thread=None,
    )
    yield
    trace_mod._state["initialized"] = True
    retry_thread = trace_mod._state["retry_thread"]
    if isinstance(retry_thread, threading.Thread):
        retry_thread.join(timeout=0.25)
    trace_mod._state.clear()
    trace_mod._state.update(
        initialized=False,
        collector_offline_reported=False,
        retry_thread=None,
    )


@pytest.fixture(autouse=True)
def _collector_up(monkeypatch: pytest.MonkeyPatch):
    """The local-collector preflight must pass by default — the tests exercise
    the exporter/init logic, not the network probe (which is covered by its own
    telemetry_otlp tests)."""
    monkeypatch.setattr(  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType]
        "shared.trace.endpoint_reachable",
        lambda _e: True,  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType]
    )


def test_disabled_returns_early(monkeypatch: pytest.MonkeyPatch):
    """When trace_enabled=False, initialize_tracing returns None and does not init the SDK."""
    monkeypatch.setattr("shared.config.settings.observability.trace_enabled", False)
    assert initialize_tracing() is None
    assert trace_mod._state["initialized"] is False


def _under_watermark(monkeypatch: pytest.MonkeyPatch) -> None:
    """Disk usage below the watermark: initialize_tracing's auto-degrade guard
    must not skip recording in tests (the dev disk can be >90% full and would
    otherwise make every init-path test environment-dependent)."""
    monkeypatch.setattr("shared.trace._disk_usage", lambda: (0.1, 100 * 1024**3))


def test_enabled_inits_traceloop_with_otlp_exporter(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """trace_enabled=True: Traceloop.init is handed an OtlpJsonHttpSpanExporter
    pointed at the LOCAL collector as the sole exporter (no api_endpoint/api_key
    network sink), batch on, traceloop's own telemetry off, plus the instruments
    set covering Anthropic/OpenAI/LangChain/Google."""
    monkeypatch.setattr("shared.config.settings.observability.trace_enabled", True)
    monkeypatch.setattr("shared.trace.traces_dir", lambda: tmp_path)
    monkeypatch.setattr(
        "shared.config.settings.observability.telemetry_otlp_endpoint",
        "http://127.0.0.1:4318",
    )
    _under_watermark(monkeypatch)

    calls: list[dict] = []  # pyright: ignore[reportMissingTypeArgument, reportUnknownVariableType]
    monkeypatch.setattr("traceloop.sdk.Traceloop.init", lambda **kw: calls.append(kw))  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType, reportUnknownMemberType]

    initialize_tracing()

    assert len(calls) == 1  # pyright: ignore[reportUnknownArgumentType]
    kw = calls[0]  # pyright: ignore[reportUnknownVariableType]
    exporter = kw["exporter"]  # pyright: ignore[reportUnknownVariableType]
    assert isinstance(exporter, OtlpJsonHttpSpanExporter)
    assert exporter._endpoint == "http://127.0.0.1:4318/v1/traces"
    assert "api_endpoint" not in kw  # no SDK-level network sink; the exporter owns the POST
    assert "api_key" not in kw
    assert kw["telemetry_enabled"] is False
    assert kw["disable_batch"] is False

    from traceloop.sdk.instruments import Instruments

    instruments = kw["instruments"]  # pyright: ignore[reportUnknownVariableType]
    assert Instruments.ANTHROPIC in instruments
    assert Instruments.OPENAI in instruments
    assert Instruments.LANGCHAIN in instruments
    assert Instruments.GOOGLE_GENERATIVEAI in instruments

    assert trace_mod._state["initialized"] is True


def test_sdk_initialize_raises_propagates_and_state_unchanged(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """If Traceloop.init() itself raises, the error propagates and _initialized stays
    False so a later retry can succeed."""
    monkeypatch.setattr("shared.config.settings.observability.trace_enabled", True)
    monkeypatch.setattr("shared.trace.traces_dir", lambda: tmp_path)
    _under_watermark(monkeypatch)

    def _raise(**_kw):  # pyright: ignore[reportMissingParameterType, reportUnknownParameterType]
        raise RuntimeError("boom: init failed")

    monkeypatch.setattr("traceloop.sdk.Traceloop.init", _raise)  # pyright: ignore[reportUnknownArgumentType]

    with pytest.raises(RuntimeError, match="boom"):
        initialize_tracing()
    assert trace_mod._state["initialized"] is False


def test_idempotent_second_call_is_noop(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Second call within the same process does not re-initialize."""
    monkeypatch.setattr("shared.config.settings.observability.trace_enabled", True)
    monkeypatch.setattr("shared.trace.traces_dir", lambda: tmp_path)
    _under_watermark(monkeypatch)
    calls: list[dict] = []  # pyright: ignore[reportMissingTypeArgument, reportUnknownVariableType]
    monkeypatch.setattr("traceloop.sdk.Traceloop.init", lambda **kw: calls.append(kw))  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType, reportUnknownMemberType]

    initialize_tracing()
    initialize_tracing()

    assert len(calls) == 1  # pyright: ignore[reportUnknownArgumentType]


def test_collector_unreachable_retries_once_until_init_succeeds(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """One daemon loop retries a collector-unreachable preflight, logs the
    episode once, and exits after tracing initializes."""
    monkeypatch.setattr("shared.config.settings.observability.trace_enabled", True)
    monkeypatch.setattr("shared.trace.traces_dir", lambda: tmp_path)
    monkeypatch.setattr("shared.trace.COLLECTOR_RETRY_INTERVAL_S", 0.1)
    _under_watermark(monkeypatch)

    reachable = iter((False, False, True))
    attempts: list[bool] = []

    def endpoint_reachable(_endpoint: str) -> bool:
        result = next(reachable)
        attempts.append(result)
        return result

    monkeypatch.setattr("shared.trace.endpoint_reachable", endpoint_reachable)
    warnings: list[str] = []

    def capture_warning(message: str, *_args: object, **_kwargs: object) -> None:
        warnings.append(message)

    monkeypatch.setattr(trace_mod.logger, "warning", capture_warning)
    initialized = threading.Event()

    def init_traceloop(**_kwargs: object) -> None:
        initialized.set()

    monkeypatch.setattr("traceloop.sdk.Traceloop.init", init_traceloop)

    initialize_tracing()
    retry_thread = trace_mod._state["retry_thread"]
    assert isinstance(retry_thread, threading.Thread)

    initialize_tracing()
    assert trace_mod._state["retry_thread"] is retry_thread
    assert initialized.wait(timeout=1.0)
    retry_thread.join(timeout=0.5)

    assert attempts == [False, False, True]
    assert warnings == ["trace recording disabled — local OTel collector not answering"]
    assert trace_mod._state["initialized"] is True
    assert trace_mod._state["collector_offline_reported"] is False
    assert not retry_thread.is_alive()


# --- OtlpJsonHttpSpanExporter ---------------------------------------------------


def test_otlp_exporter_posts_protobuf(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Each export() batch becomes one OTLP ExportTraceServiceRequest POSTed
    to <endpoint>/v1/traces with Content-Type application/x-protobuf (the wire
    format the collector's OTLP receiver accepts — its JSON receiver rejects
    the SDK's padded-base64 ids); the body parses back to the same spans."""
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor

    monkeypatch.setattr("shared.config.settings.observability.trace_strip_content", True)
    posts: list[tuple[str, bytes, dict]] = []

    class _Resp:
        def raise_for_status(self):
            pass

    def _post(url, *, content, headers, timeout):  # pyright: ignore[reportUnknownParameterType]
        posts.append((url, content, headers))  # pyright: ignore[reportUnknownArgumentType, reportUnknownMemberType]
        return _Resp()

    monkeypatch.setattr("httpx.post", _post)  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType]

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
    url, _body, headers = posts[0]  # pyright: ignore[reportUnknownVariableType]
    assert url == "http://127.0.0.1:4318/v1/traces"
    assert headers["Content-Type"] == "application/x-protobuf"

    # The body is the OTLP ExportTraceServiceRequest protobuf; it must parse
    # back to exactly the recorded spans (what Tempo ingests).
    from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest

    spans = 0
    for _, raw, _h in posts:
        req = ExportTraceServiceRequest()
        req.ParseFromString(raw)  # pyright: ignore[reportUnknownArgumentType]
        assert req.SerializeToString()
        spans += sum(len(ss.spans) for rs in req.resource_spans for ss in rs.scope_spans)
    assert spans == 2


# --- retention prune ---------------------------------------------------------


def test_prune_old_mirror_removes_stale_keeps_recent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """_prune_old_mirror deletes files older than retention_days — legacy
    `spans-YYYYMMDD-<pid>.jsonl` AND the collector's rotated
    `spans-<ISO>.jsonl` — keeps recent ones, and never touches the unstamped
    ACTIVE `spans.jsonl` or non-mirror files."""
    from shared.trace import _prune_old_mirror

    monkeypatch.setattr("shared.trace.traces_dir", lambda: tmp_path)
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


def test_prune_old_mirror_disabled_when_nonpositive(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """retention_days <= 0 disables pruning entirely."""
    from shared.trace import _prune_old_mirror

    monkeypatch.setattr("shared.trace.traces_dir", lambda: tmp_path)
    old = tmp_path / "spans-20200101-1.jsonl"
    old.write_text("{}\n", encoding="utf-8")
    _prune_old_mirror(retention_days=0)
    assert old.exists()


# --- turn_span -----------------------------------------------------------


class _FakeSpan:
    def __init__(self):
        self.attributes: dict[str, object] = {}

    def set_attribute(self, key: str, value: object) -> None:
        self.attributes[key] = value


class _FakeTracer:
    def __init__(self, span: _FakeSpan):
        self._span = span
        self.opened: list[str] = []

    @contextmanager
    def start_as_current_span(self, name: str):
        self.opened.append(name)
        yield self._span


def test_turn_span_noop_when_disabled(monkeypatch: pytest.MonkeyPatch):
    """When trace_enabled=False, turn_span is a pass-through — does not open a span."""
    monkeypatch.setattr("shared.config.settings.observability.trace_enabled", False)

    def _explode(*_a, **_kw):  # pyright: ignore[reportMissingParameterType, reportUnknownParameterType]
        raise AssertionError("get_tracer should not be called when disabled")

    monkeypatch.setattr("opentelemetry.trace.get_tracer", _explode)  # pyright: ignore[reportUnknownArgumentType]

    with turn_span(name="root", session_id="agent-42", turn=1):
        pass


def test_turn_span_noop_when_initialize_skipped(monkeypatch: pytest.MonkeyPatch):
    """Even with trace_enabled=True, if initialize_tracing hasn't run yet,
    turn_span stays no-op — otherwise it opens a span against an
    uninitialized provider."""
    monkeypatch.setattr("shared.config.settings.observability.trace_enabled", True)
    assert trace_mod._state["initialized"] is False

    def _explode(*_a, **_kw):  # pyright: ignore[reportMissingParameterType, reportUnknownParameterType]
        raise AssertionError("must not open a span when uninitialized")

    monkeypatch.setattr("opentelemetry.trace.get_tracer", _explode)  # pyright: ignore[reportUnknownArgumentType]

    with turn_span(name="root", session_id="agent-42", turn=1):
        pass


def test_turn_span_opens_root_with_session_id(monkeypatch: pytest.MonkeyPatch):
    """When enabled and initialized, turn_span opens an OTel root span with
    the given name and stamps the session and turn attributes."""
    monkeypatch.setattr("shared.config.settings.observability.trace_enabled", True)
    trace_mod._state["initialized"] = True

    span = _FakeSpan()
    tracer = _FakeTracer(span)
    monkeypatch.setattr("opentelemetry.trace.get_tracer", lambda _name: tracer)  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType]

    with turn_span(name="ava-agent-42", session_id="42", turn=3):
        pass

    assert tracer.opened == ["ava-agent-42"]
    assert span.attributes == {
        "session.id": "42",
        "ava.turn": 3,
    }


# --- trace v2: content stripping --------------------------------------------


def _otlp_with_attrs(attrs: dict[str, str]) -> dict:  # pyright: ignore[reportMissingTypeArgument, reportUnknownParameterType]
    """Build a minimal OTLP export request with one span carrying the given
    attributes (keys -> string values)."""
    return {  # pyright: ignore[reportUnknownVariableType]
        "resourceSpans": [
            {
                "scopeSpans": [
                    {
                        "spans": [
                            {
                                "name": "execute_task after_init",
                                "attributes": [
                                    {"key": k, "value": {"stringValue": v}}
                                    for k, v in attrs.items()
                                ],
                            }
                        ]
                    }
                ]
            }
        ]
    }


def test_strip_content_removes_llm_content_keeps_metadata():
    """_strip_content_attributes removes gen_ai.task.input/output,
    traceloop.entity.input/output and messages-like keys; chain metadata and
    status survive."""
    from shared.trace import _strip_content_attributes

    otlp = _otlp_with_attrs(  # pyright: ignore[reportUnknownVariableType]
        {
            "gen_ai.task.input": '{"inputs": {"messages": ["you are ava..."]}}',
            "gen_ai.task.output": "the full completion...",
            "traceloop.entity.input": '{"inputs": {...}}',
            "traceloop.entity.output": '{"outputs": {...}}',
            "messages": "[...]",
            "system_instructions": "[...]",
            "gen_ai.input.messages": "[...]",
            "gen_ai.output.messages": "[...]",
            "traceloop.association.properties.agent_id": "238",
            "traceloop.association.properties.langgraph_path": "after_init",
            "gen_ai.task.status": "success",
            "gen_ai.operation.name": "execute_task",
            "gen_ai.task.id": "019fd0ec-...",
            "session.id": "238",
        }
    )
    _strip_content_attributes(otlp)  # pyright: ignore[reportUnknownArgumentType]
    keys = {kv["key"] for kv in otlp["resourceSpans"][0]["scopeSpans"][0]["spans"][0]["attributes"]}  # pyright: ignore[reportUnknownVariableType]
    assert keys == {
        "traceloop.association.properties.agent_id",
        "traceloop.association.properties.langgraph_path",
        "gen_ai.task.status",
        "gen_ai.operation.name",
        "gen_ai.task.id",
        "session.id",
    }


def test_strip_content_size_guard_drops_huge_strings():
    """The size guard drops any single attribute whose string payload exceeds
    _MAX_ATTR_STRING_CHARS even when the key is not a known content key — a
    future instrumentor-invented content key cannot leak megabytes back."""
    from shared.trace import _MAX_ATTR_STRING_CHARS, _strip_content_attributes

    big = "x" * (_MAX_ATTR_STRING_CHARS + 1)
    otlp = _otlp_with_attrs(  # pyright: ignore[reportUnknownVariableType]
        {
            "traceloop.association.properties.agent_id": "238",
            "mystery.new.content.key": big,
        }
    )
    _strip_content_attributes(otlp)  # pyright: ignore[reportUnknownArgumentType]
    keys = {kv["key"] for kv in otlp["resourceSpans"][0]["scopeSpans"][0]["spans"][0]["attributes"]}  # pyright: ignore[reportUnknownVariableType]
    assert keys == {"traceloop.association.properties.agent_id"}


def test_strip_content_removes_event_attributes_too():
    """Content attributes nested under span events (the use_attributes=False
    path) are stripped as well."""
    from shared.trace import _strip_content_attributes

    otlp: dict[str, Any] = {
        "resourceSpans": [
            {
                "scopeSpans": [
                    {
                        "spans": [
                            {
                                "name": "chat",
                                "attributes": [
                                    {
                                        "key": "gen_ai.task.status",
                                        "value": {"stringValue": "success"},
                                    }
                                ],
                                "events": [
                                    {
                                        "name": "gen_ai.input",
                                        "attributes": [
                                            {
                                                "key": "gen_ai.input.messages",
                                                "value": {"stringValue": "[...]"},
                                            },
                                            {
                                                "key": "gen_ai.usage.input_tokens",
                                                "value": {"intValue": "42"},
                                            },
                                        ],
                                    }
                                ],
                            }
                        ]
                    }
                ]
            }
        ]
    }
    _strip_content_attributes(otlp)
    span = otlp["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
    ev_keys = {kv["key"] for kv in span["events"][0]["attributes"]}
    assert ev_keys == {"gen_ai.usage.input_tokens"}


def test_exporter_posts_stripped_line(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """OtlpJsonHttpSpanExporter strips content attributes before the POST —
    stripped here, content never reaches the sidecar, the mirror or Tempo
    (defensive layer 2)."""
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor

    monkeypatch.setattr("shared.config.settings.observability.trace_strip_content", True)
    posts: list[bytes] = []

    class _Resp:
        def raise_for_status(self):
            pass

    monkeypatch.setattr(
        "httpx.post",
        lambda _url, *, content, headers, timeout: posts.append(content) or _Resp(),  # noqa: ARG005 — signature must match httpx.post  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType]
    )

    provider = TracerProvider()
    provider.add_span_processor(
        SimpleSpanProcessor(OtlpJsonHttpSpanExporter(endpoint="http://127.0.0.1:4318"))
    )
    tracer = provider.get_tracer("ava.session")
    with tracer.start_as_current_span("ava-agent-7") as root:
        root.set_attribute("session.id", "7")
        root.set_attribute("gen_ai.task.input", "secret prompt")
        root.set_attribute("traceloop.association.properties.agent_id", "7")
    provider.shutdown()
    wire = b"".join(posts)
    assert b"secret prompt" not in wire  # content stripped before the wire
    from google.protobuf.json_format import MessageToDict
    from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest

    req = ExportTraceServiceRequest()
    req.ParseFromString(posts[0])
    keys = {
        kv["key"]
        for rs in MessageToDict(req)["resourceSpans"]
        for ss in rs["scopeSpans"]
        for sp in ss["spans"]
        for kv in sp.get("attributes", [])
    }
    assert "traceloop.association.properties.agent_id" in keys  # metadata survives


def test_exporter_strip_opt_out_keeps_content(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """trace_strip_content=False opts the wire back into full content
    (benchmarks that genuinely want prompts in Tempo/mirror)."""
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor

    monkeypatch.setattr("shared.config.settings.observability.trace_strip_content", False)
    posts: list[bytes] = []

    class _Resp:
        def raise_for_status(self):
            pass

    monkeypatch.setattr(
        "httpx.post",
        lambda _url, *, content, headers, timeout: posts.append(content) or _Resp(),  # noqa: ARG005 — signature must match httpx.post  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType]
    )

    provider = TracerProvider()
    provider.add_span_processor(
        SimpleSpanProcessor(OtlpJsonHttpSpanExporter(endpoint="http://127.0.0.1:4318"))
    )
    tracer = provider.get_tracer("ava.session")
    with tracer.start_as_current_span("root"):
        pass
    provider.shutdown()

    assert posts


# --- trace v2: file governance -----------------------------------------------


def test_enforce_dir_cap_deletes_oldest_first(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """_enforce_dir_cap deletes oldest files until the directory fits the cap."""
    from shared.trace import _enforce_dir_cap

    monkeypatch.setattr("shared.trace.traces_dir", lambda: tmp_path)
    # 4 files of 1 MB each: spans-20260101-1 .. spans-20260104-1
    for day in ("20260101", "20260102", "20260103", "20260104"):
        (tmp_path / f"spans-{day}-1.jsonl").write_bytes(b"x" * (1024 * 1024))

    removed = _enforce_dir_cap(max_mb=2)  # cap 2 MB -> delete 2 oldest
    assert removed == 2
    remaining = sorted(p.name for p in tmp_path.glob("spans*.jsonl"))
    assert remaining == ["spans-20260103-1.jsonl", "spans-20260104-1.jsonl"]


def test_enforce_dir_cap_noop_when_under_cap(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Under the cap nothing is deleted; non-positive cap disables entirely."""
    from shared.trace import _enforce_dir_cap

    monkeypatch.setattr("shared.trace.traces_dir", lambda: tmp_path)
    (tmp_path / "spans-20260101-1.jsonl").write_bytes(b"x" * 10)
    assert _enforce_dir_cap(max_mb=100) == 0
    assert len(list(tmp_path.glob("spans*.jsonl"))) == 1
    assert _enforce_dir_cap(max_mb=0) == 0
    assert len(list(tmp_path.glob("spans*.jsonl"))) == 1


def test_enforce_dir_cap_active_file_sorts_last(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """The ACTIVE spans.jsonl (no day stamp) sorts last — the cap prune never
    deletes the file the collector is appending to."""
    from shared.trace import _enforce_dir_cap

    monkeypatch.setattr("shared.trace.traces_dir", lambda: tmp_path)
    (tmp_path / "spans-20260101-1.jsonl").write_bytes(b"x" * (1024 * 1024))
    (tmp_path / "spans-2026-01-02T03-04-05.000.jsonl").write_bytes(b"x" * (1024 * 1024))
    active = tmp_path / "spans.jsonl"
    active.write_bytes(b"x" * (1024 * 1024))

    removed = _enforce_dir_cap(max_mb=2)
    assert removed == 1
    remaining = sorted(p.name for p in tmp_path.glob("spans*.jsonl"))
    assert remaining == ["spans-2026-01-02T03-04-05.000.jsonl", "spans.jsonl"], (
        "the active file must survive a cap prune"
    )


def test_disk_watermark_exceeded(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """_disk_watermark_exceeded compares the data-disk usage fraction against
    the watermark; >= 1.0 disables the guard."""
    from shared.trace import _disk_watermark_exceeded

    monkeypatch.setattr("shared.trace.traces_dir", lambda: tmp_path)
    from types import SimpleNamespace

    monkeypatch.setattr(
        "shared.trace.shutil.disk_usage",
        lambda _p: SimpleNamespace(used=50 * 4096, total=1000 * 4096, free=950 * 4096),  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType]
    )
    assert _disk_watermark_exceeded(0.9) is False
    assert _disk_watermark_exceeded(0.01) is True
    assert _disk_watermark_exceeded(1.0) is False  # guard disabled
    assert _disk_watermark_exceeded(2.0) is False


def test_initialize_sets_trace_content_false(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """initialize_tracing forces TRACELOOP_TRACE_CONTENT=false before
    Traceloop.init when strip_content is on."""
    monkeypatch.setattr("shared.config.settings.observability.trace_enabled", True)
    monkeypatch.setattr("shared.config.settings.observability.trace_strip_content", True)
    monkeypatch.setattr("shared.trace.traces_dir", lambda: tmp_path)
    _under_watermark(monkeypatch)
    monkeypatch.delenv("TRACELOOP_TRACE_CONTENT", raising=False)
    calls: list[dict] = []  # pyright: ignore[reportMissingTypeArgument, reportUnknownVariableType]
    monkeypatch.setattr("traceloop.sdk.Traceloop.init", lambda **kw: calls.append(kw))  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType, reportUnknownMemberType]

    initialize_tracing()

    assert os.environ["TRACELOOP_TRACE_CONTENT"] == "false"
    assert len(calls) == 1  # pyright: ignore[reportUnknownArgumentType]


def test_initialize_skips_when_collector_unreachable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """Local collector not answering at init -> recording stays off, no
    Traceloop.init, and a warning event carries the endpoint (the same
    init-time tradeoff the events exporter makes)."""
    monkeypatch.setattr("shared.config.settings.observability.trace_enabled", True)
    monkeypatch.setattr("shared.trace.traces_dir", lambda: tmp_path)
    monkeypatch.setattr(  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType]
        "shared.trace.endpoint_reachable",
        lambda _e: False,  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType]
    )
    monkeypatch.setattr(
        "shared.config.settings.observability.telemetry_otlp_endpoint",
        "http://127.0.0.1:4318",
    )
    _under_watermark(monkeypatch)
    calls: list[dict] = []  # pyright: ignore[reportMissingTypeArgument, reportUnknownVariableType]
    monkeypatch.setattr("traceloop.sdk.Traceloop.init", lambda **kw: calls.append(kw))  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType, reportUnknownMemberType]
    warned: list[tuple] = []  # pyright: ignore[reportMissingTypeArgument, reportUnknownVariableType]
    monkeypatch.setattr(
        "shared.trace.logger.warning",
        lambda *a, **kw: warned.append((a, kw)),  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType, reportUnknownMemberType]
    )

    initialize_tracing()

    assert calls == []
    assert trace_mod._state["initialized"] is False
    assert warned
    attrs = warned[0][1]  # pyright: ignore[reportUnknownVariableType]
    assert attrs.get("action") == "recording_disabled_collector_unreachable"  # pyright: ignore[reportUnknownMemberType]
    assert attrs.get("endpoint") == "http://127.0.0.1:4318"  # pyright: ignore[reportUnknownMemberType]


def test_initialize_skips_when_disk_over_watermark(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Disk over watermark: recording stays off, no Traceloop.init, and a
    warning telemetry event is emitted carrying the measured numbers."""
    monkeypatch.setattr("shared.config.settings.observability.trace_enabled", True)
    monkeypatch.setattr("shared.trace.traces_dir", lambda: tmp_path)
    monkeypatch.setattr("shared.trace._disk_usage", lambda: (0.951, 2 * 1024**3))
    calls: list[dict] = []  # pyright: ignore[reportMissingTypeArgument, reportUnknownVariableType]
    monkeypatch.setattr("traceloop.sdk.Traceloop.init", lambda **kw: calls.append(kw))  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType, reportUnknownMemberType]
    warned: list[tuple] = []  # pyright: ignore[reportMissingTypeArgument, reportUnknownVariableType]
    monkeypatch.setattr(
        "shared.trace.logger.warning",
        lambda *a, **kw: warned.append((a, kw)),  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType, reportUnknownMemberType]
    )

    initialize_tracing()

    assert calls == []
    assert trace_mod._state["initialized"] is False
    assert warned, "a degradation warning must be logged"
    attrs = warned[0][1]  # pyright: ignore[reportUnknownVariableType]
    assert attrs.get("event") == "trace"  # pyright: ignore[reportUnknownMemberType]
    assert attrs.get("action") == "recording_disabled_disk_watermark"  # pyright: ignore[reportUnknownMemberType]
    assert attrs.get("usage_fraction") == 0.951  # pyright: ignore[reportUnknownMemberType]
    assert attrs.get("free_gb") == 2.0  # pyright: ignore[reportUnknownMemberType]


def test_enforce_dir_cap_sorts_by_numeric_pid(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Same-day files must sort by NUMERIC pid, not string: name order would
    prune `...-1000` before `...-999`, deleting a newer file (audit 2026-08-08
    P1 — the string order also made a co-located agent's actively-written
    mirror a deletion target)."""
    from shared.trace import _enforce_dir_cap

    monkeypatch.setattr("shared.trace.traces_dir", lambda: tmp_path)
    (tmp_path / "spans-20260101-999.jsonl").write_bytes(b"x" * (1024 * 1024))
    (tmp_path / "spans-20260101-1000.jsonl").write_bytes(b"x" * (1024 * 1024))

    removed = _enforce_dir_cap(max_mb=1)
    assert removed == 1
    remaining = sorted(p.name for p in tmp_path.glob("spans*.jsonl"))
    assert remaining == ["spans-20260101-1000.jsonl"], (
        "numeric pid order must delete the older pid-999 file, not pid-1000"
    )


def test_enforce_dir_cap_survives_peer_prune(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """A file vanishing between glob and stat — a peer agent pruning the shared
    traces dir concurrently — must not raise out of the boot path (audit
    2026-08-08 P1: two bare p.stat() calls killed an agent start with
    FileNotFoundError)."""
    from pathlib import Path

    from shared.trace import _enforce_dir_cap

    monkeypatch.setattr("shared.trace.traces_dir", lambda: tmp_path)
    for day in ("20260101", "20260102", "20260103"):
        (tmp_path / f"spans-{day}-1.jsonl").write_bytes(b"x" * (1024 * 1024))

    real_stat = Path.stat
    calls = {"n": 0}

    def flaky_stat(self):  # pyright: ignore[reportMissingParameterType, reportUnknownParameterType]
        calls["n"] += 1
        if calls["n"] == 2:  # the middle file is gone by the time we stat it
            raise FileNotFoundError
        return real_stat(self)  # pyright: ignore[reportUnknownArgumentType]

    monkeypatch.setattr(Path, "stat", flaky_stat)  # pyright: ignore[reportUnknownArgumentType]
    removed = _enforce_dir_cap(max_mb=1)  # must not raise
    assert removed == 1
    remaining = sorted(p.name for p in tmp_path.glob("spans*.jsonl"))
    # the vanished file counted as 0 bytes; the sweep continued past it and
    # deleted the oldest surviving file
    assert remaining == ["spans-20260102-1.jsonl", "spans-20260103-1.jsonl"]
