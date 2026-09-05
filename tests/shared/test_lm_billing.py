"""`shared.lm.billing` contract tests for Ava's v1 billing span schema."""

from __future__ import annotations

import re
from types import SimpleNamespace
from typing import Any

import pytest
from langchain_core.messages import AIMessage

from shared.lm._plugin_providers import ensure_provider_plugins_loaded


@pytest.fixture(scope="module", autouse=True)
def _load_provider_plugins() -> None:
    ensure_provider_plugins_loaded()


class _RecordedSpan:
    def __init__(self, name: str, start_time: int | None) -> None:
        self.name = name
        self.start_time = start_time
        self.attributes: dict[str, Any] = {}
        self.ended = False

    def set_attribute(self, key: str, value: Any) -> None:
        self.attributes[key] = value

    def end(self) -> None:
        self.ended = True


class _RecordingTracer:
    def __init__(self) -> None:
        self.spans: list[_RecordedSpan] = []

    def start_span(self, name: str, *, start_time: int | None = None) -> _RecordedSpan:
        span = _RecordedSpan(name, start_time)
        self.spans.append(span)
        return span


def _enable_tracing(monkeypatch: pytest.MonkeyPatch) -> _RecordingTracer:
    from shared import trace as trace_mod

    tracer = _RecordingTracer()
    monkeypatch.setattr("shared.config.settings.observability.trace_enabled", True)
    monkeypatch.setitem(trace_mod._state, "initialized", True)
    monkeypatch.setattr("opentelemetry.trace.get_tracer", lambda _name: tracer)  # pyright: ignore[reportUnknownArgumentType]
    return tracer


def test_emit_billing_event_records_schema_v1_attributes(monkeypatch: pytest.MonkeyPatch) -> None:
    """A priced cached call is one immediately-ended billing span with the v1 fields.

    The regression this catches is a billed provider call missing an attribute,
    losing its original start time, or opening a span without closing it.
    """
    from shared.lm.billing import emit_billing_event

    tracer = _enable_tracing(monkeypatch)

    emit_billing_event(
        vendor="deepseek",
        model="deepseek-v4-pro",
        tok_in=1_000,
        tok_out=100,
        tok_cached=800,
        cost_usd=0.0003476,
        usage_kind="agent",
        start_time_ns=123_456,
    )

    assert len(tracer.spans) == 1
    span = tracer.spans[0]
    assert span.name == "ava.billing.call"
    assert span.start_time == 123_456
    assert span.ended is True
    assert {key: value for key, value in span.attributes.items() if key != "ava.billing.ts"} == {
        "ava.billing.line": "ava",
        "ava.billing.vendor": "deepseek",
        "ava.billing.model": "deepseek-v4-pro",
        "ava.billing.tokens_in": 1_000,
        "ava.billing.tokens_out": 100,
        "ava.billing.cost": 0.000348,
        "ava.billing.usage_kind": "agent",
        "ava.billing.cache_read_tokens": 800,
    }
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", span.attributes["ava.billing.ts"])


def test_emit_billing_from_message_marks_unpriced_model(monkeypatch: pytest.MonkeyPatch) -> None:
    """A known vendor without catalog pricing records an explicit free-unpriced span.

    The regression this catches is treating an unknown catalog price as a
    priced $0 call, which makes ledger gaps invisible.
    """
    from shared.lm.billing import emit_billing_from_message

    tracer = _enable_tracing(monkeypatch)
    message = AIMessage(
        content="answer",
        usage_metadata={"input_tokens": 20, "output_tokens": 5, "total_tokens": 25},
    )

    emit_billing_from_message(message, model="deepseek-not-priced", usage_kind="chat")

    assert len(tracer.spans) == 1
    assert tracer.spans[0].attributes["ava.billing.vendor"] == "deepseek"
    assert tracer.spans[0].attributes["ava.billing.cost"] == 0.0
    assert tracer.spans[0].attributes["ava.billing.unpriced"] is True


def test_emit_billing_from_message_skips_missing_usage_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A response with no standardized token usage produces no billing span.

    The regression this catches is converting unknown usage into a misleading
    zero-token ledger event.
    """
    from shared.lm.billing import emit_billing_from_message

    tracer = _enable_tracing(monkeypatch)

    emit_billing_from_message(
        AIMessage(content="answer"), model="deepseek-v4-pro", usage_kind="chat"
    )

    assert tracer.spans == []


@pytest.mark.parametrize(
    ("model", "vendor"),
    [
        ("claude-sonnet-4", "anthropic"),
        ("deepseek-v4-pro", "deepseek"),
        ("gemini-3.5-flash", "google"),
        ("gpt-5.6-sol", "openai"),
        ("mimo-v2", "xiaomi"),
        ("kimi-k2.7-code", "moonshot"),
        ("glm-5", "zhipu"),
        ("qwen3.8-max", "alibaba"),
        # Alibaba family ids nest directly after the stem; a bare dash prefix
        # would miss them, and a non-Alibaba "qwenfoo-*" must not be attributed.
        ("qwenfoo-fast", None),
        ("unregistered-model", None),
    ],
)
def test_vendor_of_model_recognizes_registered_core_and_plugin_prefixes(
    model: str,
    vendor: str | None,
) -> None:
    """Vendor attribution uses registered manufacturer identities without guessing."""
    from shared.lm.billing import vendor_of_model

    assert vendor_of_model(model) == vendor


def test_vendor_of_model_uses_registered_plugin_display_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A registered plugin provider contributes its lowercase manufacturer name.

    The regression this catches is silently skipping a billable plugin call
    after provider registration has made the model invokable.
    """
    from shared.lm import provider_api
    from shared.lm.billing import vendor_of_model

    monkeypatch.setitem(
        provider_api.REGISTRY.bindings,
        "acme-",
        SimpleNamespace(display_name="Acme AI"),
    )

    assert vendor_of_model("acme-fast") == "acme ai"


def test_emit_billing_event_is_noop_when_tracing_is_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Disabling tracing prevents even tracer acquisition on the hot path.

    The regression this catches is the observability kill switch still
    allocating billing spans.
    """
    from shared.lm.billing import emit_billing_event

    tracer = _RecordingTracer()
    monkeypatch.setattr("shared.config.settings.observability.trace_enabled", False)
    monkeypatch.setattr("opentelemetry.trace.get_tracer", lambda _name: tracer)  # pyright: ignore[reportUnknownArgumentType]

    emit_billing_event(
        vendor="deepseek",
        model="deepseek-v4-pro",
        tok_in=1,
        tok_out=1,
        cost_usd=0.0,
        usage_kind="chat",
    )

    assert tracer.spans == []


def test_emit_billing_event_absorbs_tracing_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """A tracing failure cannot turn a completed provider call into an error.

    The regression this catches is observability availability changing the
    caller's successful LLM behavior.
    """
    from shared.lm.billing import emit_billing_event

    _enable_tracing(monkeypatch)

    def _broken_tracer(_name: str) -> _RecordingTracer:
        raise RuntimeError("tracing is unavailable")

    monkeypatch.setattr("opentelemetry.trace.get_tracer", _broken_tracer)

    emit_billing_event(
        vendor="deepseek",
        model="deepseek-v4-pro",
        tok_in=1,
        tok_out=1,
        cost_usd=0.0,
        usage_kind="chat",
    )
