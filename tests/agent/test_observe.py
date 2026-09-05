"""`agent.observe.log_llm_usage` field format guard.

Blows up here when LangChain changes the usage_metadata shape — caught earlier than
missing numbers on the dashboard. Consistent across providers: langchain-deepseek /
langchain-anthropic / langchain-openai all plumb cache + reasoning figures into
usage_metadata.{input,output}_token_details.
"""

from datetime import UTC, datetime
from typing import Any

import pytest
from langchain_core.messages import AIMessage

from agent.observe import log_llm_usage


def _msgs(records: list[dict]) -> str:
    return "\n".join(r["message"] for r in records)  # pyright: ignore[reportUnknownArgumentType]


def test_deepseek_shape_logs(loguru_records):
    msg = AIMessage(
        content="",
        usage_metadata={
            "input_tokens": 1000,
            "output_tokens": 100,
            "total_tokens": 1100,
            "input_token_details": {"cache_read": 800},
            "output_token_details": {"reasoning": 70},
        },
    )
    log_llm_usage(msg, model="deepseek-v4-pro")
    out = _msgs(loguru_records)  # pyright: ignore[reportUnknownArgumentType]
    assert "in=1000 cached=800" in out
    assert "(80%)" in out
    assert "out=100 reason=70" in out
    assert "(70%)" in out
    # model goes into extra (not the message string) → PostgresSink writes payload->>'model',
    # dashboard 24h cost groups/prices by it. If the field name drifts, this test goes red.
    assert loguru_records[0]["extra"]["model"] == "deepseek-v4-pro"
    assert loguru_records[0]["extra"]["usage_kind"] == "agent"


def test_anthropic_shape_logs(loguru_records):
    """When usage_metadata has no output_token_details (e.g. no thinking
    enabled), reason=0 is still logged. With ThinkingTokensChatAnthropic,
    thinking-enabled calls surface thinking_tokens as output_token_details.reasoning."""
    msg = AIMessage(
        content="",
        usage_metadata={
            "input_tokens": 2000,
            "output_tokens": 50,
            "total_tokens": 2050,
            "input_token_details": {"cache_read": 1500, "cache_creation": 500},
            # no output_token_details
        },
    )
    log_llm_usage(msg, model="claude-opus-4-7")
    out = _msgs(loguru_records)  # pyright: ignore[reportUnknownArgumentType]
    assert "in=2000 cached=1500" in out
    assert "out=50 reason=0" in out


def test_zero_input_no_pct(loguru_records):
    msg = AIMessage(
        content="",
        usage_metadata={
            "input_tokens": 0,
            "output_tokens": 0,
            "total_tokens": 0,
        },
    )
    log_llm_usage(msg, model="claude-opus-4-7")
    out = _msgs(loguru_records)  # pyright: ignore[reportUnknownArgumentType]
    assert "in=0" in out
    assert "%" not in out


def test_latency_ms_rides_payload(loguru_records):
    """latency_ms goes into the event payload (extra) so the ops monitor panel
    can read it back — not into the message string. None -> payload null."""
    msg = AIMessage(
        content="",
        usage_metadata={"input_tokens": 100, "output_tokens": 10, "total_tokens": 110},
    )
    log_llm_usage(msg, model="deepseek-v4-pro", latency_ms=1234.5)
    out = _msgs(loguru_records)  # pyright: ignore[reportUnknownArgumentType]
    assert "in=100" in out
    assert "latency" not in out  # never in the human-readable line
    assert loguru_records[0]["extra"]["latency_ms"] == 1234.5
    loguru_records.clear()  # pyright: ignore[reportUnknownMemberType]
    log_llm_usage(msg, model="deepseek-v4-pro")
    assert loguru_records[0]["extra"]["latency_ms"] is None


def test_decode_ms_rides_payload(loguru_records):
    """decode_ms goes into the event payload (extra) — the generation-stage
    TPS panel sums it per bucket — never into the human-readable line.
    None -> payload null (non-streaming fallback / pre-instrumentation rows)."""
    msg = AIMessage(
        content="",
        usage_metadata={"input_tokens": 100, "output_tokens": 10, "total_tokens": 110},
    )
    log_llm_usage(msg, model="deepseek-v4-pro", latency_ms=1234.5, decode_ms=800.0)
    out = _msgs(loguru_records)  # pyright: ignore[reportUnknownArgumentType]
    assert "in=100" in out
    assert "decode" not in out  # never in the human-readable line
    assert loguru_records[0]["extra"]["decode_ms"] == 800.0
    assert loguru_records[0]["extra"]["latency_ms"] == 1234.5
    loguru_records.clear()  # pyright: ignore[reportUnknownMemberType]
    log_llm_usage(msg, model="deepseek-v4-pro", latency_ms=1234.5)
    assert loguru_records[0]["extra"]["decode_ms"] is None


def test_task_id_is_logged_only_for_explicit_task_context(loguru_records) -> None:
    """Untagged work must not silently inherit an owner's task budget."""
    msg = AIMessage(
        content="",
        usage_metadata={"input_tokens": 100, "output_tokens": 10, "total_tokens": 110},
    )
    log_llm_usage(msg, model="deepseek-v4-pro", task_id=42)
    assert loguru_records[0]["extra"]["task_id"] == 42
    loguru_records.clear()  # pyright: ignore[reportUnknownMemberType]

    log_llm_usage(msg, model="deepseek-v4-pro")
    assert "task_id" not in loguru_records[0]["extra"]


def test_no_usage_metadata_silent(loguru_records):
    msg = AIMessage(content="")
    log_llm_usage(msg, model="claude-opus-4-7")
    assert loguru_records == []


def test_price_snapshot_rides_payload(loguru_records):
    """The usage-time price snapshot (user principle, task #1273): cost_usd
    plus the three per-1M rates ride the event payload at write time, so the
    read side never re-prices against the current catalog. At the fixed
    off-peak instant deepseek-v4-pro is 0.66 / 0.022 / 1.98 USD/M: in=1000
    cached=800 out=100 -> $0.0003476."""
    msg = AIMessage(
        content="",
        usage_metadata={
            "input_tokens": 1000,
            "output_tokens": 100,
            "total_tokens": 1100,
            "input_token_details": {"cache_read": 800},
        },
    )
    log_llm_usage(
        msg,
        model="deepseek-v4-pro",
        priced_at=datetime(2026, 8, 17, 0, 0, tzinfo=UTC),
    )
    extra = loguru_records[0]["extra"]
    assert extra["cost_usd"] == pytest.approx(0.0003476)  # pyright: ignore[reportUnknownMemberType]
    assert extra["price_miss"] == 0.66
    assert extra["price_hit"] == 0.022
    assert extra["price_out"] == 1.98
    # never in the human-readable line
    assert "cost" not in loguru_records[0]["message"]


def test_log_llm_usage_emits_agent_billing_span(
    monkeypatch: pytest.MonkeyPatch,
    loguru_records: list[dict[str, Any]],
) -> None:
    """A streamed agent response emits its quoted usage as one billing span.

    The regression this catches is a log-only price snapshot that leaves the
    billing ledger without the completed provider call.
    """
    from opentelemetry import trace as otel_trace

    from shared import trace as trace_mod
    from shared.lm.pricing import quote

    class _Span:
        def __init__(self, start_time: int | None) -> None:
            self.start_time = start_time
            self.attributes: dict[str, Any] = {}

        def set_attribute(self, key: str, value: Any) -> None:
            self.attributes[key] = value

        def end(self) -> None:
            pass

    class _Tracer:
        def __init__(self) -> None:
            self.spans: list[_Span] = []

        def start_span(self, _name: str, *, start_time: int | None = None) -> _Span:
            span = _Span(start_time)
            self.spans.append(span)
            return span

    tracer = _Tracer()
    priced_at = datetime(2026, 8, 17, 0, 0, tzinfo=UTC)
    expected = quote("deepseek-v4-pro", 1_000, 100, 800, at=priced_at)
    assert expected is not None
    monkeypatch.setattr("shared.config.settings.observability.trace_enabled", True)
    monkeypatch.setitem(trace_mod._state, "initialized", True)
    monkeypatch.setattr(otel_trace, "get_tracer", lambda _name: tracer)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr("time.time_ns", lambda: 5_000_000_000)
    message = AIMessage(
        content="",
        usage_metadata={
            "input_tokens": 1_000,
            "output_tokens": 100,
            "total_tokens": 1_100,
            "input_token_details": {"cache_read": 800},
        },
    )

    log_llm_usage(
        message,
        model="deepseek-v4-pro",
        latency_ms=1_234.5,
        priced_at=priced_at,
    )

    assert len(tracer.spans) == 1
    span = tracer.spans[0]
    assert span.start_time == 3_765_500_000
    assert span.attributes["ava.billing.vendor"] == "deepseek"
    assert span.attributes["ava.billing.usage_kind"] == "agent"
    assert span.attributes["ava.billing.tokens_in"] == 1_000
    assert span.attributes["ava.billing.tokens_out"] == 100
    assert span.attributes["ava.billing.cache_read_tokens"] == 800
    assert span.attributes["ava.billing.cost"] == round(expected.cost_usd, 6)
    assert loguru_records[0]["extra"]["cost_usd"] == pytest.approx(expected.cost_usd)  # pyright: ignore[reportUnknownMemberType]


def test_log_llm_usage_skips_billing_when_usage_metadata_is_incomplete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Incomplete standardized usage remains observable but is not billable.

    The regression this catches is a provider response lacking one token total
    becoming a misleading partial or zero-token billing ledger event.
    """
    from opentelemetry import trace as otel_trace

    from shared import trace as trace_mod

    class _Span:
        def set_attribute(self, _key: str, _value: Any) -> None:
            pass

        def end(self) -> None:
            pass

    class _Tracer:
        def __init__(self) -> None:
            self.spans: list[_Span] = []

        def start_span(self, _name: str, *, start_time: int | None = None) -> _Span:
            span = _Span()
            self.spans.append(span)
            return span

    tracer = _Tracer()
    monkeypatch.setattr("shared.config.settings.observability.trace_enabled", True)
    monkeypatch.setitem(trace_mod._state, "initialized", True)
    monkeypatch.setattr(otel_trace, "get_tracer", lambda _name: tracer)  # pyright: ignore[reportUnknownArgumentType]
    message = AIMessage(
        content="",
        usage_metadata={"input_tokens": 100, "output_tokens": 0, "total_tokens": 100},
    )
    object.__setattr__(message, "usage_metadata", {"input_tokens": 100, "total_tokens": 100})

    log_llm_usage(message, model="deepseek-v4-pro")

    assert tracer.spans == []


def test_price_snapshot_absent_for_unpriced_model(
    loguru_records: list[dict[str, Any]],
) -> None:
    """A model with no known price emits NO snapshot fields — absent means
    unpriced (the readers count such calls as unpriced_calls instead of
    billing them at 0). A null cost_usd would be ambiguous with a $0 call.
    The warning makes the catalog gap actionable instead of silently accruing
    more unpriced ledger rows."""
    msg = AIMessage(
        content="",
        usage_metadata={"input_tokens": 100, "output_tokens": 10, "total_tokens": 110},
    )
    log_llm_usage(msg, model="no-such-model")
    usage_record = next(
        record for record in loguru_records if record["extra"].get("event") == "llm_usage"
    )
    extra = usage_record["extra"]
    assert "cost_usd" not in extra
    assert "price_miss" not in extra
    warnings = [record for record in loguru_records if record["level"].name == "WARNING"]
    assert len(warnings) == 1
    warning = warnings[0]["message"]
    assert "no-such-model" in warning
    assert "shared/lm/pricing_catalog_archive.json" in warning
    assert "plugin price registry" in warning
