"""Billing-span emission for individual provider LLM calls."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from base.config import settings
from base.telemetry import report_sink_failure

AVA_BILLING_ATTR_LINE = "ava.billing.line"
AVA_BILLING_ATTR_VENDOR = "ava.billing.vendor"
AVA_BILLING_ATTR_MODEL = "ava.billing.model"
AVA_BILLING_ATTR_TOKENS_IN = "ava.billing.tokens_in"
AVA_BILLING_ATTR_TOKENS_OUT = "ava.billing.tokens_out"
AVA_BILLING_ATTR_COST = "ava.billing.cost"
AVA_BILLING_ATTR_USAGE_KIND = "ava.billing.usage_kind"
AVA_BILLING_ATTR_TS = "ava.billing.ts"
AVA_BILLING_ATTR_CACHE_READ_TOKENS = "ava.billing.cache_read_tokens"
AVA_BILLING_ATTR_CACHE_WRITE_5M_TOKENS = "ava.billing.cache_write_5m_tokens"
AVA_BILLING_ATTR_CACHE_WRITE_1H_TOKENS = "ava.billing.cache_write_1h_tokens"
AVA_BILLING_ATTR_UNPRICED = "ava.billing.unpriced"

_CORE_VENDOR_PREFIXES: tuple[tuple[str, str], ...] = ()


def _is_qwen_family(model: str) -> bool:
    """Alibaba family ids nest directly after the stem (``qwen3.8-max``), so a
    bare ``qwen-`` prefix would miss them. Require a digit after the stem (or an
    exact ``qwen`` id) so a non-Alibaba ``qwenfoo-*`` id cannot be mis-attributed;
    plugin registration already rejects such prefixes, this is a second guard.
    """
    return model == "qwen" or (model.startswith("qwen") and len(model) > 4 and model[4].isdigit())


def vendor_of_model(model: str) -> str | None:
    """Return the registered manufacturer for ``model``, if one is known."""
    for prefix, vendor in _CORE_VENDOR_PREFIXES:
        if model.startswith(prefix):
            return vendor
    if _is_qwen_family(model):
        return "alibaba"

    from base.lm.plugin_providers import model_catalog

    for prefix, binding in model_catalog().bindings.items():
        if model.startswith(prefix):
            return binding.display_name.lower()
    return None


def _utc_timestamp() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def emit_billing_event(
    *,
    vendor: str,
    model: str,
    tok_in: int,
    tok_out: int,
    cost_usd: float,
    usage_kind: str,
    line: str = "ava",
    tok_cached: int = 0,
    cache_write_5m: int = 0,
    cache_write_1h: int = 0,
    unpriced: bool = False,
    start_time_ns: int | None = None,
) -> None:
    """End one trace-safe billing span for a completed provider call.

    Billing must never affect the call it observes, including while tracing is
    initializing or an exporter is unavailable.
    """
    try:
        if not settings.observability.trace_enabled:
            return

        from base.telemetry import tracing

        tracing.ensure_init_resolved()
        if not tracing.is_initialized():
            return

        from opentelemetry import trace as otel_trace

        span = otel_trace.get_tracer("ava.billing").start_span(
            "ava.billing.call",
            start_time=start_time_ns,
        )
        span.set_attribute(AVA_BILLING_ATTR_LINE, line)
        span.set_attribute(AVA_BILLING_ATTR_VENDOR, vendor)
        span.set_attribute(AVA_BILLING_ATTR_MODEL, model)
        span.set_attribute(AVA_BILLING_ATTR_TOKENS_IN, int(tok_in))
        span.set_attribute(AVA_BILLING_ATTR_TOKENS_OUT, int(tok_out))
        span.set_attribute(AVA_BILLING_ATTR_COST, round(float(cost_usd), 6))
        span.set_attribute(AVA_BILLING_ATTR_USAGE_KIND, usage_kind)
        span.set_attribute(AVA_BILLING_ATTR_TS, _utc_timestamp())
        if tok_cached:
            span.set_attribute(AVA_BILLING_ATTR_CACHE_READ_TOKENS, int(tok_cached))
        if cache_write_5m:
            span.set_attribute(AVA_BILLING_ATTR_CACHE_WRITE_5M_TOKENS, cache_write_5m)
        if cache_write_1h:
            span.set_attribute(AVA_BILLING_ATTR_CACHE_WRITE_1H_TOKENS, cache_write_1h)
        if unpriced:
            span.set_attribute(AVA_BILLING_ATTR_UNPRICED, unpriced)
        span.end()
    except Exception as exc:
        report_sink_failure("billing span emission (the span is dropped)", exc)


def emit_billing_from_message(
    msg: Any,
    *,
    model: str,
    usage_kind: str,
    line: str = "ava",
    start_time_ns: int | None = None,
    vendor: str | None = None,
) -> None:
    """Price a LangChain message and emit its billing span when usage is known."""
    try:
        from base.lm.pricing import quote, tally_tokens
        from base.lm.pricing.cache_writes import cache_write_tokens
        from base.lm.usage import usage_model

        tok_in, tok_out, tok_cached = tally_tokens([msg])
        if tok_in is None or tok_out is None or tok_cached is None:
            return
        model = usage_model(msg, model)
        resolved_vendor = vendor or vendor_of_model(model)
        if resolved_vendor is None:
            return
        write_5m, write_1h = cache_write_tokens(msg.usage_metadata.get("input_token_details") or {})
        priced = quote(
            model,
            tok_in,
            tok_out,
            tok_cached,
            cache_write_5m=write_5m,
            cache_write_1h=write_1h,
        )
        emit_billing_event(
            line=line,
            vendor=resolved_vendor,
            model=model,
            tok_in=tok_in,
            tok_out=tok_out,
            tok_cached=tok_cached,
            cache_write_5m=write_5m,
            cache_write_1h=write_1h,
            cost_usd=priced.cost_usd if priced is not None else 0.0,
            usage_kind=usage_kind,
            unpriced=priced is None,
            start_time_ns=start_time_ns,
        )
    except Exception as exc:
        report_sink_failure("pricing a provider message for billing (its span is dropped)", exc)
