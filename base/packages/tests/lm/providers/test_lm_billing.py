"""`base.lm.usage` contract tests for Meiva's v1 billing span schema."""

from __future__ import annotations

import re
from typing import Any

import pytest
from langchain_core.language_models.fake_chat_models import FakeListChatModel
from langchain_core.messages import AIMessage

from base import config
from base.lm.catalog import ModelCatalog
from base.lm.provider_api import ProviderBinding
from base.packages.tests.model_catalog import AddBindings


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
    from base.telemetry import tracing as tracing_mod

    tracer = _RecordingTracer()
    monkeypatch.setattr(config.settings.observability, "trace_enabled", True)
    monkeypatch.setattr(tracing_mod, "is_initialized", lambda: True)
    monkeypatch.setattr("opentelemetry.trace.get_tracer", lambda _name: tracer)  # pyright: ignore[reportUnknownArgumentType]
    return tracer


def test_emit_billing_event_records_schema_v1_attributes(monkeypatch: pytest.MonkeyPatch) -> None:
    """A priced cached call is one immediately-ended billing span with the v1 fields.

    The regression this catches is a billed provider call missing an attribute,
    losing its original start time, or opening a span without closing it.
    """
    from base.lm.usage import emit_billing_event

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
    assert span.name == "meiva.billing.call"
    assert span.start_time == 123_456
    assert span.ended is True
    assert {key: value for key, value in span.attributes.items() if key != "meiva.billing.ts"} == {
        "meiva.billing.line": "meiva",
        "meiva.billing.vendor": "deepseek",
        "meiva.billing.model": "deepseek-v4-pro",
        "meiva.billing.tokens_in": 1_000,
        "meiva.billing.tokens_out": 100,
        "meiva.billing.cost": 0.000348,
        "meiva.billing.usage_kind": "agent",
        "meiva.billing.cache_read_tokens": 800,
    }
    assert re.fullmatch(
        r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", span.attributes["meiva.billing.ts"]
    )


@pytest.mark.parametrize("path", ["message", "durable"])
def test_message_billing_accounts_for_both_cache_write_ttls(
    model_catalog: ModelCatalog,
    monkeypatch: pytest.MonkeyPatch,
    path: str,
) -> None:
    from base.lm.usage import emit_billing_from_message, log_usage_from_message

    tracer = _enable_tracing(monkeypatch)
    message = AIMessage(
        content="answer",
        usage_metadata={
            "input_tokens": 1000,
            "output_tokens": 50,
            "total_tokens": 1050,
            "input_token_details": {
                "cache_read": 200,
                "ephemeral_5m_input_tokens": 300,
                "ephemeral_1h_input_tokens": 400,
            },
        },
    )
    if path == "message":
        emit_billing_from_message(
            message, model="claude-opus-5-5", usage_kind="chat", catalog=model_catalog
        )
    else:
        log_usage_from_message(
            message, model="claude-opus-5-5", usage_kind="agent", catalog=model_catalog
        )
    assert len(tracer.spans) == 1
    attrs = tracer.spans[0].attributes
    assert attrs["meiva.billing.cache_write_5m_tokens"] == 300
    assert attrs["meiva.billing.cache_write_1h_tokens"] == 400
    assert attrs["meiva.billing.cost"] == pytest.approx(0.00614)


def test_emit_billing_from_message_marks_unpriced_model(
    model_catalog: ModelCatalog, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A known vendor without catalog pricing records an explicit free-unpriced span.

    The regression this catches is treating an unknown catalog price as a
    priced $0 call, which makes ledger gaps invisible.
    """
    from base.lm.usage import emit_billing_from_message

    tracer = _enable_tracing(monkeypatch)
    message = AIMessage(
        content="answer",
        usage_metadata={"input_tokens": 20, "output_tokens": 5, "total_tokens": 25},
    )

    emit_billing_from_message(
        message, model="deepseek-not-priced", usage_kind="chat", catalog=model_catalog
    )

    assert len(tracer.spans) == 1
    assert tracer.spans[0].attributes["meiva.billing.vendor"] == "deepseek"
    assert tracer.spans[0].attributes["meiva.billing.cost"] == 0.0
    assert tracer.spans[0].attributes["meiva.billing.unpriced"] is True


def test_emit_billing_from_message_skips_missing_usage_metadata(
    model_catalog: ModelCatalog,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A response with no standardized token usage produces no billing span.

    The regression this catches is converting unknown usage into a misleading
    zero-token ledger event.
    """
    from base.lm.usage import emit_billing_from_message

    tracer = _enable_tracing(monkeypatch)

    emit_billing_from_message(
        AIMessage(content="answer"),
        model="deepseek-v4-pro",
        usage_kind="chat",
        catalog=model_catalog,
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
    model_catalog: ModelCatalog,
    model: str,
    vendor: str | None,
) -> None:
    """Vendor attribution uses registered manufacturer identities without guessing."""
    from base.lm.usage import vendor_of_model

    assert vendor_of_model(model, catalog=model_catalog) == vendor


def test_vendor_of_model_uses_registered_plugin_display_name(
    model_catalog: ModelCatalog, add_bindings: AddBindings
) -> None:
    """A registered plugin provider contributes its lowercase manufacturer name.

    The regression this catches is silently skipping a billable plugin call
    after provider registration has made the model invokable.
    """
    from base.lm.usage import vendor_of_model

    model_catalog = add_bindings(
        model_catalog,
        {
            "acme-": ProviderBinding(
                prefix="acme-",
                display_name="Acme AI",
                key_env="ACME_API_KEY",
                build=lambda _ctx: FakeListChatModel(responses=["unused"]),
            )
        },
    )

    assert vendor_of_model("acme-fast", catalog=model_catalog) == "acme ai"


def test_emit_billing_event_is_noop_when_tracing_is_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Disabling tracing prevents even tracer acquisition on the hot path.

    The regression this catches is the observability kill switch still
    allocating billing spans.
    """
    from base.lm.usage import emit_billing_event

    tracer = _RecordingTracer()
    monkeypatch.setattr(config.settings.observability, "trace_enabled", False)
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
    from base.lm.usage import emit_billing_event

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


@pytest.mark.parametrize("path", ["message", "durable"])
@pytest.mark.parametrize("cached", [None, 0])
def test_billing_distinguishes_unmetered_cache_from_reported_zero(
    model_catalog: ModelCatalog,
    monkeypatch: pytest.MonkeyPatch,
    path: str,
    cached: int | None,
) -> None:
    from base.lm.usage import emit_billing_from_message, log_usage_from_message

    tracer = _enable_tracing(monkeypatch)
    message = AIMessage(
        content="answer",
        usage_metadata={
            "input_tokens": 1000,
            "output_tokens": 50,
            "total_tokens": 1050,
            "input_token_details": {} if cached is None else {"cache_read": cached},
        },
    )
    if path == "message":
        emit_billing_from_message(
            message, model="deepseek-flash", usage_kind="chat", catalog=model_catalog
        )
    else:
        log_usage_from_message(
            message, model="deepseek-flash", usage_kind="chat", catalog=model_catalog
        )
    [span] = tracer.spans
    assert span.attributes["meiva.billing.tokens_in"] == 1000
    assert span.attributes["meiva.billing.cost"] > 0
    if cached is None:
        assert "meiva.billing.cache_read_tokens" not in span.attributes
    else:
        assert span.attributes["meiva.billing.cache_read_tokens"] == 0
