"""Tests for `ava trace ship` — replay the local trace mirror to a viewer."""

from __future__ import annotations

import json
from pathlib import Path
from typing import cast

import httpx
import pytest

from cli.commands.trace import (
    TraceShipError,
    _load_watermark,
    _post_line,
    _ShipTarget,
    cmd_trace_ship,
)


def test_load_watermark_drops_entries_for_missing_files(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """Retention prunes mirror files; the watermark must shed their stale entries."""
    monkeypatch.setattr("cli.commands.trace.traces_dir", lambda: tmp_path)
    (tmp_path / "spans.jsonl").write_text("x\n", encoding="utf-8")
    (tmp_path / ".ship-watermark.json").write_text(
        json.dumps({"spans.jsonl": 2, "spans-20200101-9.jsonl": 5}),
        encoding="utf-8",
    )
    marks = _load_watermark()
    assert marks == {"spans.jsonl": 2}


def _enable_tempo_config(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr("shared.config.settings.observability.telemetry_otlp_enabled", True)
    monkeypatch.setattr(
        "shared.config.settings.observability.telemetry_tempo_endpoint", "http://tempo.test:14318"
    )
    monkeypatch.setattr(
        "cli.commands.trace.machine_role", lambda: frozenset({"gateway", "agent-runner"})
    )


def _otlp_line(span_name: str) -> str:
    """One OTLP-JSON ExportTraceServiceRequest line, exactly as the exporter writes."""
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor, SpanExporter, SpanExportResult

    captured: list = []

    class _Cap(SpanExporter):
        def export(self, spans):
            captured.extend(spans)  # pyright: ignore[reportUnknownMemberType]
            return SpanExportResult.SUCCESS

        def shutdown(self):
            pass

    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(_Cap()))
    with provider.get_tracer("t").start_as_current_span(span_name):
        pass

    from google.protobuf.json_format import MessageToDict
    from opentelemetry.exporter.otlp.proto.common.trace_encoder import encode_spans

    return json.dumps(MessageToDict(encode_spans(captured)), separators=(",", ":"))  # pyright: ignore[reportUnknownArgumentType]


def test_post_line_converts_otlp_json_hex_ids_to_protobuf_bytes() -> None:
    """OTLP/JSON IDs are hex, even though protobuf JSON treats bytes as base64."""
    trace_id = "0123456789abcdef0123456789abcdef"
    span_id = "0123456789abcdef"
    posts: list[bytes] = []

    class _Response:
        def raise_for_status(self) -> None:
            return None

    class _Client:
        def post(self, _endpoint: str, *, content: bytes, headers: dict[str, str]) -> _Response:
            posts.append(content)
            return _Response()

    line = json.dumps(
        {
            "resourceSpans": [
                {
                    "scopeSpans": [
                        {
                            "spans": [
                                {
                                    "traceId": trace_id,
                                    "spanId": span_id,
                                    "parentSpanId": span_id,
                                }
                            ]
                        }
                    ]
                }
            ]
        }
    )

    client = cast(httpx.Client, _Client())
    assert (
        _post_line(
            client,
            _ShipTarget(endpoint="http://tempo.test/v1/traces", headers={}, label="tempo"),
            line,
        )
        == 1
    )

    from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest

    request = ExportTraceServiceRequest()
    request.ParseFromString(posts[0])
    span = request.resource_spans[0].scope_spans[0].spans[0]
    assert span.trace_id == bytes.fromhex(trace_id)
    assert span.span_id == bytes.fromhex(span_id)
    assert span.parent_span_id == bytes.fromhex(span_id)


def test_ship_disabled_raises(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """The OTLP kill switch off -> fail fast, the shipper is unconfigured."""
    monkeypatch.setattr("shared.trace_mirror.traces_dir", lambda: tmp_path)
    monkeypatch.setattr("cli.commands.trace.traces_dir", lambda: tmp_path)
    monkeypatch.setattr("shared.config.settings.observability.telemetry_otlp_enabled", False)
    with pytest.raises(TraceShipError, match="AVA_TELEMETRY_OTLP_ENABLED"):
        cmd_trace_ship(since=None, until=None, dry_run=False)


def test_ship_dry_run_needs_no_config(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """--dry-run reads the mirror only: no backend config required, nothing
    POSTed, counts reported — the inspection path when the kill switch is off."""
    monkeypatch.setattr("cli.commands.trace.traces_dir", lambda: tmp_path)
    (tmp_path / "spans.jsonl").write_text(
        _otlp_line("a") + "\n" + _otlp_line("b") + "\n", encoding="utf-8"
    )
    (tmp_path / "spans-2026-06-16T00-00-00.000.jsonl").write_text(
        _otlp_line("c") + "\n", encoding="utf-8"
    )
    posts: list = []

    class _Client:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

        def post(self, endpoint, *, content, headers):
            posts.append(content)  # pyright: ignore[reportUnknownArgumentType, reportUnknownMemberType]

    monkeypatch.setattr("httpx.Client", _Client)

    assert cmd_trace_ship(since=None, until=None, dry_run=True) == 0
    assert posts == []  # dry run never POSTs
    assert not (tmp_path / ".ship-watermark.json").exists()  # watermark untouched


def test_incremental_ship_posts_and_advances_watermark(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """Incremental ship POSTs each line, then a second run sends nothing new."""
    _enable_tempo_config(monkeypatch)
    monkeypatch.setattr("cli.commands.trace.traces_dir", lambda: tmp_path)

    (tmp_path / "spans.jsonl").write_text(
        _otlp_line("a") + "\n" + _otlp_line("b") + "\n", encoding="utf-8"
    )

    posts: list[bytes] = []

    class _Resp:
        def raise_for_status(self):
            pass

    class _Client:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

        def post(self, endpoint, *, content, headers):
            posts.append(content)  # pyright: ignore[reportUnknownArgumentType]
            return _Resp()

    monkeypatch.setattr("httpx.Client", _Client)

    assert cmd_trace_ship(since=None, until=None, dry_run=False) == 0
    assert len(posts) == 2  # one POST per OTLP-JSON line

    wm = json.loads((tmp_path / ".ship-watermark.json").read_text())
    assert wm["spans.jsonl"] > 0

    # Second run: watermark is at EOF, nothing re-ships.
    posts.clear()
    assert cmd_trace_ship(since=None, until=None, dry_run=False) == 0
    assert posts == []


def test_incremental_ship_reads_gzipped_segments_and_keeps_watermark(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """A segment compressed by the agent-side pass (.jsonl.gz) is decompressed
    on read, keyed in the watermark by the BASE name (so the gzip rename never
    strands or re-ships lines), and a second run posts nothing new."""
    import gzip as gz

    _enable_tempo_config(monkeypatch)
    monkeypatch.setattr("cli.commands.trace.traces_dir", lambda: tmp_path)

    rotated = tmp_path / "spans-2026-08-27T03-29-10.942-size.jsonl.gz"
    with gz.open(rotated, "wt", encoding="utf-8") as f:
        f.write(_otlp_line("a") + "\n")
        f.write(_otlp_line("b") + "\n")

    posts: list[bytes] = []

    class _Resp:
        def raise_for_status(self):
            pass

    class _Client:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

        def post(self, endpoint, *, content, headers):
            posts.append(content)  # pyright: ignore[reportUnknownArgumentType]
            return _Resp()

    monkeypatch.setattr("httpx.Client", _Client)

    assert cmd_trace_ship(since=None, until=None, dry_run=False) == 0
    assert len(posts) == 2  # both gz lines POSTed

    # Watermark keyed by the base name — the same key a plain segment used.
    wm = json.loads((tmp_path / ".ship-watermark.json").read_text())
    assert wm["spans-2026-08-27T03-29-10.942-size.jsonl"] > 0
    assert "spans-2026-08-27T03-29-10.942-size.jsonl.gz" not in wm

    # Second run: watermark is at the end of the stream, nothing re-ships.
    posts.clear()
    assert cmd_trace_ship(since=None, until=None, dry_run=False) == 0
    assert posts == []


def test_incremental_ship_gz_resumes_from_partial_watermark(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """A gz segment whose base-name watermark sits mid-stream ships only the
    unshipped tail (the shipped prefix is discarded, not re-POSTed)."""
    import gzip as gz

    _enable_tempo_config(monkeypatch)
    monkeypatch.setattr("cli.commands.trace.traces_dir", lambda: tmp_path)

    rotated = tmp_path / "spans-2026-08-27T03-29-10.942-size.jsonl.gz"
    with gz.open(rotated, "wt", encoding="utf-8") as f:
        f.write(_otlp_line("a") + "\n")
        f.write(_otlp_line("b") + "\n")

    # Pre-seed the watermark as if "a" was already shipped (byte offset of the
    # second line = length of the first line's bytes).
    first = _otlp_line("a") + "\n"
    (tmp_path / ".ship-watermark.json").write_text(
        json.dumps({"spans-2026-08-27T03-29-10.942-size.jsonl": len(first.encode())}),
        encoding="utf-8",
    )

    posts: list[bytes] = []

    class _Resp:
        def raise_for_status(self):
            pass

    class _Client:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

        def post(self, endpoint, *, content, headers):
            posts.append(content)  # pyright: ignore[reportUnknownArgumentType]
            return _Resp()

    monkeypatch.setattr("httpx.Client", _Client)

    assert cmd_trace_ship(since=None, until=None, dry_run=False) == 0
    assert len(posts) == 1  # only the tail line


def test_windowed_ship_includes_gzipped_segments(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Windowed ship scans gz segments too — a gzipped window is not stranded
    by the compression pass."""
    import gzip as gz

    _enable_tempo_config(monkeypatch)
    monkeypatch.setattr("cli.commands.trace.traces_dir", lambda: tmp_path)

    gz_rotated = tmp_path / "spans-2026-06-16T00-00-00.000-size.jsonl.gz"
    with gz.open(gz_rotated, "wt", encoding="utf-8") as f:
        f.write(_otlp_line("c") + "\n")
    out_of_window = tmp_path / "spans-2020-06-16T00-00-00.000-size.jsonl"
    out_of_window.write_text(_otlp_line("x") + "\n", encoding="utf-8")

    posts: list[bytes] = []

    class _Resp:
        def raise_for_status(self):
            pass

    class _Client:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

        def post(self, endpoint, *, content, headers):
            posts.append(content)  # pyright: ignore[reportUnknownArgumentType]
            return _Resp()

    monkeypatch.setattr("httpx.Client", _Client)

    assert cmd_trace_ship(since="2026-06-15", until="2026-06-17", dry_run=False) == 0
    assert len(posts) == 1  # only the gz segment in the window


def test_ship_client_bypasses_environment_proxy(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """The ship client must never route through a host system proxy: the macOS
    system-config branch httpx reads with trust_env=True sends protobuf POSTs to
    the VPN/clash proxy (127.0.0.1:7897), which mangles binary bodies and answers
    502 — a two-day real outage of the trace replay pipeline (2026-08-25)."""
    _enable_tempo_config(monkeypatch)
    monkeypatch.setattr("cli.commands.trace.traces_dir", lambda: tmp_path)
    (tmp_path / "spans.jsonl").write_text(_otlp_line("a") + "\n", encoding="utf-8")

    seen: dict[str, object] = {}

    class _Resp:
        def raise_for_status(self):
            pass

    class _Client:
        def __init__(self, *a: object, **k: object):
            seen.update(k)

        def __enter__(self):
            return self

        def __exit__(self, *a: object):
            pass

        def post(self, endpoint, *, content, headers):
            return _Resp()

    monkeypatch.setattr("httpx.Client", _Client)

    assert cmd_trace_ship(since=None, until=None, dry_run=False) == 0
    assert seen.get("trust_env") is False


def test_windowed_ship_filters_by_file_day_and_ignores_watermark(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """--since/--until selects files by day stamp and ships them whole, regardless
    of any incremental watermark."""
    _enable_tempo_config(monkeypatch)
    monkeypatch.setattr("cli.commands.trace.traces_dir", lambda: tmp_path)

    (tmp_path / "spans-20260610-1.jsonl").write_text(_otlp_line("old") + "\n", encoding="utf-8")
    (tmp_path / "spans-2026-06-16T03-04-05.000.jsonl").write_text(
        _otlp_line("new") + "\n", encoding="utf-8"
    )
    # Pre-existing watermark at EOF for the in-window file — windowed mode ignores it.
    (tmp_path / ".ship-watermark.json").write_text(
        json.dumps({"spans-2026-06-16T03-04-05.000.jsonl": 99999}), encoding="utf-8"
    )

    posts: list[bytes] = []

    class _Resp:
        def raise_for_status(self):
            pass

    class _Client:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

        def post(self, endpoint, *, content, headers):
            posts.append(content)  # pyright: ignore[reportUnknownArgumentType]
            return _Resp()

    monkeypatch.setattr("httpx.Client", _Client)

    assert cmd_trace_ship(since="2026-06-15", until="2026-06-17", dry_run=False) == 0
    assert len(posts) == 1  # only the in-window file, shipped whole despite the watermark


def test_gateway_ship_posts_to_local_tempo_without_auth(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A gateway replays straight to its loopback Tempo, bypassing its local
    collector because that collector would mirror the replay again."""
    monkeypatch.setattr("shared.trace_mirror.traces_dir", lambda: tmp_path)  # pyright: ignore[reportUnknownMemberType]
    monkeypatch.setattr("cli.commands.trace.traces_dir", lambda: tmp_path)  # pyright: ignore[reportUnknownMemberType]
    monkeypatch.setattr("shared.config.settings.observability.telemetry_otlp_enabled", True)  # pyright: ignore[reportUnknownMemberType]
    monkeypatch.setattr(  # pyright: ignore[reportUnknownMemberType]
        "shared.config.settings.observability.telemetry_tempo_endpoint", "http://tempo.test:14318"
    )
    monkeypatch.setattr(  # pyright: ignore[reportUnknownMemberType]
        "cli.commands.trace.machine_role", lambda: frozenset({"gateway"})
    )

    (tmp_path / "spans.jsonl").write_text(  # pyright: ignore[reportUnknownMemberType]
        _otlp_line("a") + "\n" + _otlp_line("b") + "\n", encoding="utf-8"
    )

    posts: list[tuple[str, dict[str, str]]] = []

    class _Resp:
        def raise_for_status(self) -> None:
            return None

    class _Client:
        def __init__(self, *_a: object, **_kw: object) -> None:
            return None

        def __enter__(self) -> _Client:
            return self

        def __exit__(self, *_a: object) -> None:
            return None

        def post(self, endpoint: str, *, content: bytes, headers: dict[str, str]) -> _Resp:
            posts.append((endpoint, headers))
            return _Resp()

    monkeypatch.setattr("httpx.Client", _Client)  # pyright: ignore[reportUnknownMemberType]

    assert cmd_trace_ship(since=None, until=None, dry_run=False) == 0
    assert len(posts) == 2  # pyright: ignore[reportUnknownArgumentType]
    for endpoint, headers in posts:
        assert endpoint == "http://tempo.test:14318/v1/traces"
        assert "Authorization" not in headers
        assert headers["Content-Type"] == "application/x-protobuf"

    wm = json.loads((tmp_path / ".ship-watermark.json").read_text())  # pyright: ignore[reportUnknownArgumentType, reportUnknownMemberType]
    assert wm["spans.jsonl"] > 0


def test_runner_ship_posts_to_gateway_relay_with_cluster_bearer(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A pure runner cannot dial loopback Tempo. It replays to the gateway's
    authenticated remote trace pipeline, which deliberately has no file exporter."""
    monkeypatch.setattr("cli.commands.trace.traces_dir", lambda: tmp_path)  # pyright: ignore[reportUnknownMemberType]
    monkeypatch.setattr(  # pyright: ignore[reportUnknownMemberType]
        "shared.config.settings.observability.telemetry_otlp_enabled", True
    )
    monkeypatch.setattr(  # pyright: ignore[reportUnknownMemberType]
        "shared.config.settings.gateway.gateway_url", "http://10.0.0.10:8000"
    )
    monkeypatch.setattr(  # pyright: ignore[reportUnknownMemberType]
        "shared.config.settings.data_plane.cluster_secret", "cluster-token"
    )
    monkeypatch.setattr(  # pyright: ignore[reportUnknownMemberType]
        "cli.commands.trace.machine_role", lambda: frozenset({"agent-runner"})
    )
    (tmp_path / "spans.jsonl").write_text(_otlp_line("remote") + "\n", encoding="utf-8")  # pyright: ignore[reportUnknownMemberType]

    posts: list[tuple[str, dict[str, str]]] = []

    class _Resp:
        def raise_for_status(self) -> None:
            return None

    class _Client:
        def __init__(self, *_a: object, **_kw: object) -> None:
            return None

        def __enter__(self) -> _Client:
            return self

        def __exit__(self, *_a: object) -> None:
            return None

        def post(self, endpoint: str, *, content: bytes, headers: dict[str, str]) -> _Resp:
            posts.append((endpoint, headers))
            return _Resp()

    monkeypatch.setattr("httpx.Client", _Client)  # pyright: ignore[reportUnknownMemberType]

    assert cmd_trace_ship(since=None, until=None, dry_run=False) == 0
    assert posts == [
        (
            "http://10.0.0.10:4318/v1/traces",
            {
                "Content-Type": "application/x-protobuf",
                "Authorization": "Bearer cluster-token",
            },
        )
    ]
