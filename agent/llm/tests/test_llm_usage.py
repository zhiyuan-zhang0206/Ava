"""`agent.llm.usage.log_llm_usage` field format guard.

Blows up here when LangChain changes the usage_metadata shape — caught earlier than
missing numbers on the dashboard. Consistent across providers: langchain-deepseek /
langchain-anthropic / langchain-openai all plumb cache + reasoning figures into
usage_metadata.{input,output}_token_details.
"""

from datetime import UTC, datetime
from typing import Any

import pytest
from langchain_core.messages import AIMessage

from agent.llm.usage import log_llm_usage
from base.lm.plugin_providers import model_catalog


@pytest.fixture(scope="module", autouse=True)
def _load_provider_plugins() -> None:
    """Provider vendor attribution needs the plugin registry populated; without
    it `vendor_of_model` returns None, the billing span is skipped, and
    order-dependent failures appear when this module runs standalone."""
    model_catalog()


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


def test_usage_has_no_task_attribution(loguru_records) -> None:
    """Task notes do not claim ownership of an agent's token consumption."""
    msg = AIMessage(
        content="", usage_metadata={"input_tokens": 100, "output_tokens": 10, "total_tokens": 110}
    )
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

    from base.lm.pricing import quote
    from base.telemetry import tracing as tracing_mod

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
    monkeypatch.setattr("base.config.settings.observability.trace_enabled", True)
    monkeypatch.setattr(tracing_mod, "is_initialized", lambda: True)
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

    from base.telemetry import tracing as tracing_mod

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
    monkeypatch.setattr("base.config.settings.observability.trace_enabled", True)
    monkeypatch.setattr(tracing_mod, "is_initialized", lambda: True)
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
    assert "base/lm/pricing/pricing_catalog_archive.json" in warning
    assert "plugin price registry" in warning


def test_gemini_explicit_cache_provenance_labels(loguru_records: list[dict[str, Any]]):
    """cache_mechanism/cache_scope ride the event extra when the call site
    knows the request rode the Gemini explicit cache (task #2660) — the API
    then reports only the explicit block, so the event must say so instead of
    letting the dashboard misread the share as full-prefix."""
    msg = AIMessage(
        content="",
        usage_metadata={
            "input_tokens": 1000,
            "output_tokens": 100,
            "total_tokens": 1100,
            "input_token_details": {"cache_read": 200},
        },
    )
    log_llm_usage(
        msg,
        model="gemini-3.8-flash",
        cache_mechanism="mixed",
        cache_scope="explicit_block",
    )
    extra = loguru_records[0]["extra"]
    assert extra["cache_mechanism"] == "mixed"
    assert extra["cache_scope"] == "explicit_block"


def test_no_provenance_labels_when_unknown(loguru_records: list[dict[str, Any]]):
    """Without labels the event carries no cache_mechanism/cache_scope key —
    an honest 'unknown' rather than a fabricated one."""
    msg = AIMessage(
        content="",
        usage_metadata={
            "input_tokens": 1000,
            "output_tokens": 100,
            "total_tokens": 1100,
            "input_token_details": {"cache_read": 200},
        },
    )
    log_llm_usage(msg, model="gemini-3.8-flash")
    assert "cache_mechanism" not in loguru_records[0]["extra"]
    assert "cache_scope" not in loguru_records[0]["extra"]


_EVENT_KEYS = (
    "model",
    "in_total",
    "out_total",
    "cache_read",
    "cache_write_5m",
    "cache_write_1h",
    "reasoning",
    "cost_usd",
    "price_miss",
    "price_hit",
    "price_out",
    "price_write_5m",
    "price_write_1h",
)


def test_message_is_stamped_with_exactly_the_logged_figures(loguru_records):
    msg = AIMessage(
        content="",
        usage_metadata={
            "input_tokens": 3000,
            "output_tokens": 80,
            "total_tokens": 3080,
            "input_token_details": {
                "cache_read": 1500,
                "ephemeral_5m_input_tokens": 400,
                "ephemeral_1h_input_tokens": 100,
            },
            "output_token_details": {"reasoning": 20},
        },
    )
    log_llm_usage(msg, model="claude-opus-4-7")
    event: dict[str, Any] = loguru_records[0]["extra"]
    stamped = msg.additional_kwargs["ava_usage"]
    assert stamped["cost_usd"] > 0
    assert (stamped["cache_write_5m"], stamped["cache_write_1h"]) == (400, 100)
    for key in _EVENT_KEYS:
        assert stamped.get(key) == event.get(key), key
    assert set(stamped) <= set(_EVENT_KEYS) | {"unpriced"}


def test_non_agent_logging_does_not_stamp(loguru_records):
    from base.lm.usage import log_usage_from_message

    msg = AIMessage(
        content="",
        usage_metadata={"input_tokens": 10, "output_tokens": 1, "total_tokens": 11},
    )
    log_usage_from_message(msg, "claude-opus-4-7", usage_kind="label")
    assert "ava_usage" not in msg.additional_kwargs
