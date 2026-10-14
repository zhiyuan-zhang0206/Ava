"""Tracing cases: strip content removes llm content keeps metadata."""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from base import config
from base.log.tests.log_records import LogRecords
from base.log.tests.log_records import loguru_records as loguru_records
from base.paths import traces_dir
from base.telemetry import trace_mirror
from base.telemetry import tracing as tracing_mod
from base.telemetry.tests.otlp.identity_inputs import ignore_arguments, machine_identity
from base.telemetry.tests.test_tracing import collector_up as collector_up
from base.telemetry.tests.test_tracing import (
    fixed_disk_usage,
    otlp_with_attrs,
    under_watermark,
    wait_init_resolved,
    warned_fields,
)
from base.telemetry.tests.test_tracing import (
    production_process_by_default as production_process_by_default,
)
from base.telemetry.tests.test_tracing import (
    reset_tracing_init_flag as reset_tracing_init_flag,
)
from base.telemetry.tracing import OtlpJsonHttpSpanExporter, initialize_tracing


def test_strip_content_removes_llm_content_keeps_metadata():
    """strip_content_attributes removes gen_ai.task.input/output,
    traceloop.entity.input/output and messages-like keys; chain metadata and
    status survive."""
    from base.telemetry.tracing import strip_content_attributes

    otlp = otlp_with_attrs(
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
    strip_content_attributes(otlp)  # pyright: ignore[reportUnknownArgumentType]
    keys = {kv["key"] for kv in otlp["resourceSpans"][0]["scopeSpans"][0]["spans"][0]["attributes"]}
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
    MAX_ATTR_STRING_CHARS even when the key is not a known content key — a
    future instrumentor-invented content key cannot leak megabytes back."""
    from base.telemetry.tracing import MAX_ATTR_STRING_CHARS, strip_content_attributes

    big = "x" * (MAX_ATTR_STRING_CHARS + 1)
    otlp = otlp_with_attrs(
        {
            "traceloop.association.properties.agent_id": "238",
            "mystery.new.content.key": big,
        }
    )
    strip_content_attributes(otlp)  # pyright: ignore[reportUnknownArgumentType]
    keys = {kv["key"] for kv in otlp["resourceSpans"][0]["scopeSpans"][0]["spans"][0]["attributes"]}
    assert keys == {"traceloop.association.properties.agent_id"}


def test_strip_content_removes_event_attributes_too():
    """Content attributes nested under span events (the use_attributes=False
    path) are stripped as well."""
    from base.telemetry.tracing import strip_content_attributes

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
    strip_content_attributes(otlp)
    span = otlp["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
    ev_keys = {kv["key"] for kv in span["events"][0]["attributes"]}
    assert ev_keys == {"gen_ai.usage.input_tokens"}


def test_exporter_posts_stripped_line(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """OtlpJsonHttpSpanExporter strips content attributes before the POST —
    stripped here, content never reaches the sidecar, the mirror or Tempo
    (defensive layer 2)."""
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor

    monkeypatch.setattr(config.settings.observability, "trace_strip_content", True)
    posts: list[bytes] = []

    class _Resp:
        def raise_for_status(self):
            pass

    monkeypatch.setattr(
        "httpx.post",
        lambda _url, *, content, headers, timeout: posts.append(content) or _Resp(),  # noqa: ARG005 — signature must match httpx.post  # pyright: ignore[reportUnknownArgumentType]
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

    monkeypatch.setattr(config.settings.observability, "trace_strip_content", False)
    posts: list[bytes] = []

    class _Resp:
        def raise_for_status(self):
            pass

    monkeypatch.setattr(
        "httpx.post",
        lambda _url, *, content, headers, timeout: posts.append(content) or _Resp(),  # noqa: ARG005 — signature must match httpx.post  # pyright: ignore[reportUnknownArgumentType]
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


def test_enforce_dir_cap_deletes_oldest_first(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """enforce_dir_cap deletes oldest files until the directory fits the cap."""
    from base.telemetry.tracing import enforce_dir_cap

    # 4 files of 1 MB each: one rotated backup per day, 2026-01-01 .. 2026-01-04
    for day in ("2026-01-01", "2026-01-02", "2026-01-03", "2026-01-04"):
        (tmp_path / f"spans-{day}T00-00-00.000-size.jsonl").write_bytes(b"x" * (1024 * 1024))

    removed = enforce_dir_cap(max_mb=2, traces_dir=tmp_path)  # cap 2 MB -> delete 2 oldest
    assert removed == 2
    remaining = sorted(p.name for p in tmp_path.glob("spans*.jsonl"))
    assert remaining == [
        "spans-2026-01-03T00-00-00.000-size.jsonl",
        "spans-2026-01-04T00-00-00.000-size.jsonl",
    ]


def test_enforce_dir_cap_noop_when_under_cap(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Under the cap nothing is deleted; non-positive cap disables entirely."""
    from base.telemetry.tracing import enforce_dir_cap

    (tmp_path / "spans-2026-01-01T00-00-00.000-size.jsonl").write_bytes(b"x" * 10)
    assert enforce_dir_cap(max_mb=100, traces_dir=tmp_path) == 0
    assert len(list(tmp_path.glob("spans*.jsonl"))) == 1
    assert enforce_dir_cap(max_mb=0, traces_dir=tmp_path) == 0
    assert len(list(tmp_path.glob("spans*.jsonl"))) == 1


def test_enforce_dir_cap_active_file_sorts_last(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """The ACTIVE spans.jsonl (no day stamp) sorts last — the cap prune never
    deletes the file the collector is appending to."""
    from base.telemetry.tracing import enforce_dir_cap

    (tmp_path / "spans-2026-01-01T00-00-00.000-size.jsonl").write_bytes(b"x" * (1024 * 1024))
    (tmp_path / "spans-2026-01-02T03-04-05.000-size.jsonl").write_bytes(b"x" * (1024 * 1024))
    active = tmp_path / "spans.jsonl"
    active.write_bytes(b"x" * (1024 * 1024))

    removed = enforce_dir_cap(max_mb=2, traces_dir=tmp_path)
    assert removed == 1
    remaining = sorted(p.name for p in tmp_path.glob("spans*.jsonl"))
    assert remaining == ["spans-2026-01-02T03-04-05.000-size.jsonl", "spans.jsonl"], (
        "the active file must survive a cap prune"
    )


def test_disk_watermark_exceeded(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """disk_watermark_exceeded compares the data-disk usage fraction against
    the watermark; >= 1.0 disables the guard."""
    from base.telemetry.tracing import disk_watermark_exceeded

    monkeypatch.setattr(
        trace_mirror.shutil,
        "disk_usage",
        lambda _p: SimpleNamespace(used=50 * 4096, total=1000 * 4096, free=950 * 4096),  # pyright: ignore[reportUnknownArgumentType]
    )
    assert disk_watermark_exceeded(0.9, traces_dir=tmp_path) is False
    assert disk_watermark_exceeded(0.01, traces_dir=tmp_path) is True
    assert disk_watermark_exceeded(1.0, traces_dir=tmp_path) is False  # guard disabled
    assert disk_watermark_exceeded(2.0, traces_dir=tmp_path) is False


def test_initialize_relief_pass_runs_when_disk_over_watermark(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """The bounded-disk pass (gzip / retention / cap) runs BEFORE the
    watermark guard: an over-watermark disk still gets its relief pass — the
    stale segment is pruned, the gzip and cap legs are invoked, and recording
    itself is skipped (auto-degrade, watermark guard)."""
    from base.telemetry.tracing import initialize_tracing

    monkeypatch.setattr(config.settings.observability, "trace_enabled", True)
    monkeypatch.setattr(config.settings.observability, "trace_strip_content", True)
    monkeypatch.setattr(config.settings.observability, "trace_retention_days", 14)
    monkeypatch.setattr(config.settings.observability, "trace_max_dir_mb", 2048)
    # Disk OVER the watermark: recording must be skipped...
    monkeypatch.setattr(tracing_mod, "disk_usage", fixed_disk_usage(0.99, 10 * 1024**3))
    mirror = traces_dir(tmp_path)
    mirror.mkdir()
    old = mirror / "spans-2020-01-01T00-00-00.000-size.jsonl"
    old.write_text("{}\n", encoding="utf-8")
    # Spy on the gzip and cap legs (the prune leg is exercised for real): all
    # three must be reached before the watermark guard returns. The cap spy
    # records the setting it was invoked with.
    gzip_calls: list[int] = []
    cap_calls: list[int] = []

    def _spy_gzip(**_kw: object) -> int:
        gzip_calls.append(1)
        return 0

    def _spy_cap(max_mb: int, **_kw: object) -> int:
        cap_calls.append(max_mb)
        return 0

    monkeypatch.setattr(tracing_mod, "gzip_old_mirror", _spy_gzip)
    monkeypatch.setattr(tracing_mod, "enforce_dir_cap", _spy_cap)
    init_calls: list[dict[str, object]] = []
    monkeypatch.setattr(
        "traceloop.sdk.Traceloop.init",
        lambda **kw: init_calls.append(kw),  # pyright: ignore[reportUnknownArgumentType]
    )

    initialize_tracing(machine=machine_identity(), home=tmp_path)

    # ...but the relief pass still ran: the stale segment is pruned, the gzip
    # and cap legs were invoked, and recording itself was skipped.
    assert not old.exists()
    assert gzip_calls == [1]
    assert cap_calls == [2048]  # the cap leg ran, with the configured cap
    assert init_calls == []


def test_initialize_sets_trace_content_false(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """initialize_tracing forces TRACELOOP_TRACE_CONTENT=false before
    Traceloop.init when strip_content is on."""
    monkeypatch.setattr(config.settings.observability, "trace_enabled", True)
    monkeypatch.setattr(config.settings.observability, "trace_strip_content", True)
    under_watermark(monkeypatch)
    calls: list[dict] = []
    monkeypatch.setattr("traceloop.sdk.Traceloop.init", lambda **kw: calls.append(kw))  # pyright: ignore[reportUnknownArgumentType, reportUnknownMemberType]

    initialize_tracing(machine=machine_identity(), home=tmp_path)
    wait_init_resolved()

    assert os.environ["TRACELOOP_TRACE_CONTENT"] == "false"
    assert len(calls) == 1  # pyright: ignore[reportUnknownArgumentType]


def test_initialize_skips_when_collector_unreachable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, loguru_records: LogRecords
):
    """Local collector not answering at init -> recording stays off, no
    Traceloop.init, and a warning event carries the endpoint (the same
    init-time tradeoff the events exporter makes)."""
    monkeypatch.setattr(config.settings.observability, "trace_enabled", True)
    monkeypatch.setattr(
        tracing_mod,
        "endpoint_reachable",
        lambda _e: False,  # pyright: ignore[reportUnknownArgumentType]
    )
    monkeypatch.setattr(
        config.settings.observability,
        "telemetry_otlp_endpoint",
        "http://127.0.0.1:4318",
    )
    under_watermark(monkeypatch)
    # This test exercises the skip contract, not the daemon retry loop (that
    # is test_collector_unreachable_retries_once_until_init_succeeds); without
    # the stub the daemon retry thread (300s sleep) leaks across tests.
    monkeypatch.setattr(tracing_mod, "start_collector_retry", ignore_arguments)
    calls: list[dict] = []
    monkeypatch.setattr("traceloop.sdk.Traceloop.init", lambda **kw: calls.append(kw))  # pyright: ignore[reportUnknownArgumentType, reportUnknownMemberType]
    initialize_tracing(machine=machine_identity(), home=tmp_path)

    assert calls == []
    assert tracing_mod._state["initialized"] is False
    warned = warned_fields(loguru_records)
    assert warned
    assert warned[0].get("action") == "recording_disabled_collector_unreachable"
    assert warned[0].get("endpoint") == "http://127.0.0.1:4318"


def test_initialize_skips_when_disk_over_watermark(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, loguru_records: LogRecords
):
    """Disk over watermark: recording stays off, no Traceloop.init, and a
    warning telemetry event is emitted carrying the measured numbers."""
    monkeypatch.setattr(config.settings.observability, "trace_enabled", True)
    monkeypatch.setattr(tracing_mod, "disk_usage", fixed_disk_usage(0.951, 2 * 1024**3))
    calls: list[dict] = []
    monkeypatch.setattr("traceloop.sdk.Traceloop.init", lambda **kw: calls.append(kw))  # pyright: ignore[reportUnknownArgumentType, reportUnknownMemberType]
    initialize_tracing(machine=machine_identity(), home=tmp_path)

    assert calls == []
    assert tracing_mod._state["initialized"] is False
    warned = warned_fields(loguru_records)
    assert warned, "a degradation warning must be logged"
    attrs = warned[0]
    assert attrs.get("event") == "trace"
    assert attrs.get("action") == "recording_disabled_disk_watermark"
    assert attrs.get("usage_fraction") == 0.951
    assert attrs.get("free_gb") == 2.0


def test_enforce_dir_cap_survives_peer_prune(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """A file vanishing between glob and stat — a peer agent pruning the shared
    traces dir concurrently — must not raise out of the boot path (audit
    2026-08-08 P1: two bare p.stat() calls killed an agent start with
    FileNotFoundError)."""
    from pathlib import Path

    from base.telemetry.tracing import enforce_dir_cap

    for day in ("2026-01-01", "2026-01-02", "2026-01-03"):
        (tmp_path / f"spans-{day}T00-00-00.000-size.jsonl").write_bytes(b"x" * (1024 * 1024))

    real_stat = Path.stat
    calls = {"n": 0}

    def flaky_stat(self):
        calls["n"] += 1
        if calls["n"] == 2:  # the middle file is gone by the time we stat it
            raise FileNotFoundError
        return real_stat(self)  # pyright: ignore[reportUnknownArgumentType]

    monkeypatch.setattr(Path, "stat", flaky_stat)  # pyright: ignore[reportUnknownArgumentType]
    removed = enforce_dir_cap(max_mb=1, traces_dir=tmp_path)  # must not raise
    assert removed == 1
    remaining = sorted(p.name for p in tmp_path.glob("spans*.jsonl"))
    # the vanished file counted as 0 bytes; the sweep continued past it and
    # deleted the oldest surviving file
    assert remaining == [
        "spans-2026-01-02T00-00-00.000-size.jsonl",
        "spans-2026-01-03T00-00-00.000-size.jsonl",
    ]
