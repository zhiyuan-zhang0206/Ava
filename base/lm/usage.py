"""Canonical durable usage accounting and billing spans for completed LLM calls."""

from __future__ import annotations

import time
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from langchain_core.messages import AIMessage

from base.config import settings
from base.lm.catalog import ModelCatalog
from base.lm.content import message_addl_kwargs
from base.lm.pricing import CostQuote, quote, tally_tokens
from base.lm.pricing.cache_writes import cache_write_tokens
from base.lm.provider_api import InferenceSpeed
from base.log import logger
from base.telemetry import report_sink_failure


def usage_model(msg: AIMessage, requested_model: str, *, catalog: ModelCatalog) -> str:
    """Resolve the billable Meiva ID from a Fast call's actual service receipt.

    Preserve the requested ID for ordinary services. An explicit standard
    downgrade uses the standard ID; a missing or unknown Fast receipt raises
    rather than fabricating premium consumption.
    """
    spec = catalog.models.get(requested_model)
    if spec is None or spec.fast_of is None:
        return requested_model
    binding = next(
        b for prefix, b in catalog.bindings.items() if requested_model.startswith(prefix)
    )
    if binding.served_speed is None:
        raise ValueError(f"Fast model {requested_model!r} has no served_speed adapter")
    speed = InferenceSpeed(binding.served_speed(msg.response_metadata))
    return requested_model if speed == InferenceSpeed.FAST else spec.fast_of


def _metered_cache_read(usage: Mapping[str, Any], cached: int) -> int | None:
    """Distinguish absent provider metering from an explicitly reported zero."""
    details: Mapping[str, Any] = usage.get("input_token_details") or {}
    return (
        cached
        if any(key == "cache_read" or key.endswith("_cache_read") for key in details)
        else None
    )


def log_usage_from_message(
    msg: AIMessage,
    model: str,
    *,
    latency_ms: float | None = None,
    decode_ms: float | None = None,
    priced_at: datetime | None = None,
    usage_kind: str = "agent",
    source: str | None = None,
    for_agent_id: int | None = None,
    stamp_message: bool = False,
    catalog: ModelCatalog,
) -> tuple[int, float] | None:
    """Log one completed LangChain message's token usage and price snapshot.

    `stamp_message` also writes the logged figures onto `msg.additional_kwargs["meiva_usage"]`
    (`MeivaUsage`): the one `quote` result feeds the event and the message alike.
    """

    if not isinstance(msg, AIMessage):
        return None
    usage_metadata = msg.usage_metadata
    if not usage_metadata:
        return None
    try:
        in_total, out_total, cache_read = tally_tokens([msg])
    except KeyError:
        # Preserve main-conversation observability for a malformed provider
        # usage payload, while withholding its incomplete billing span.
        in_total = usage_metadata.get("input_tokens", 0) or 0
        out_total = usage_metadata.get("output_tokens", 0) or 0
        cache_read = (usage_metadata.get("input_token_details") or {}).get("cache_read", 0) or 0
    if in_total is None or out_total is None or cache_read is None:
        return None
    cache_read = _metered_cache_read(usage_metadata, cache_read)
    from base.lm.reasoning import extract_reasoning_tokens

    reasoning = extract_reasoning_tokens(msg.usage_metadata, content=msg.content)  # pyright: ignore[reportUnknownArgumentType, reportUnknownMemberType]
    write_5m, write_1h = cache_write_tokens(usage_metadata.get("input_token_details") or {})
    record: dict[str, Any] = {}
    logged = _log_usage(
        usage_model(msg, model, catalog=catalog),
        in_total=in_total,
        out_total=out_total,
        cache_read=cache_read,
        cache_write_5m=write_5m,
        cache_write_1h=write_1h,
        reasoning=reasoning,
        latency_ms=latency_ms,
        decode_ms=decode_ms,
        priced_at=priced_at,
        usage_kind=usage_kind,
        source=source,
        for_agent_id=for_agent_id,
        emit_billing="input_tokens" in usage_metadata and "output_tokens" in usage_metadata,
        requested_model=model,
        record_out=record,
        catalog=catalog,
    )
    if stamp_message:
        message_addl_kwargs(msg)["meiva_usage"] = record
    return logged


def log_usage_fields(
    model: str,
    *,
    tok_in: int,
    tok_out: int,
    tok_cached: int | None = None,
    cache_write_5m: int = 0,
    cache_write_1h: int = 0,
    tok_reasoning: int = 0,
    latency_ms: float | None = None,
    usage_kind: str,
    source: str | None = None,
    for_agent_id: int | None = None,
    catalog: ModelCatalog,
) -> tuple[int, float]:
    """Log raw provider usage; absent cache metering stays absent, including on decisions."""
    return _log_usage(
        model,
        in_total=tok_in,
        out_total=tok_out,
        cache_read=tok_cached,
        cache_write_5m=cache_write_5m,
        cache_write_1h=cache_write_1h,
        reasoning=tok_reasoning,
        latency_ms=latency_ms,
        usage_kind=usage_kind,
        source=source,
        for_agent_id=for_agent_id,
        catalog=catalog,
    )


def _price_snapshot(priced: CostQuote) -> dict[str, float]:
    return {
        "cost_usd": priced.cost_usd,
        "price_miss": priced.rates.cache_miss,
        "price_hit": priced.rates.cache_hit,
        "price_out": priced.rates.output,
        **{
            key: value
            for key, value in (
                ("price_write_5m", priced.rates.cache_write_5m),
                ("price_write_1h", priced.rates.cache_write_1h),
            )
            if value is not None
        },
    }


def _cache_usage(
    cache_read: int | None, in_total: int, write_5m: int, write_1h: int
) -> tuple[dict[str, int], str]:
    """Only measured cache counts enter events or their human-readable usage line."""
    fields: dict[str, int] = {}
    text = ""
    if cache_read is not None:
        fields["cache_read"] = cache_read
        text = f" cached={cache_read}"
        if in_total:
            text += f" ({cache_read / in_total * 100:.0f}%)"
    if cache_read is not None or write_5m or write_1h:
        fields.update(cache_write_5m=write_5m, cache_write_1h=write_1h)
    return fields, text


def _log_usage(
    model: str,
    *,
    in_total: int,
    out_total: int,
    cache_read: int | None,
    reasoning: int,
    latency_ms: float | None,
    usage_kind: str,
    decode_ms: float | None = None,
    priced_at: datetime | None = None,
    source: str | None = None,
    for_agent_id: int | None = None,
    emit_billing: bool = True,
    requested_model: str | None = None,
    cache_write_5m: int = 0,
    cache_write_1h: int = 0,
    record_out: dict[str, Any] | None = None,
    catalog: ModelCatalog,
) -> tuple[int, float]:
    """Emit one priced or explicitly unpriced usage event and billing span."""
    cache_fields, cache_text = _cache_usage(cache_read, in_total, cache_write_5m, cache_write_1h)
    reason_pct = f" ({reasoning / out_total * 100:.0f}%)" if out_total else ""
    priced = quote(
        model,
        in_total,
        out_total,
        cache_read,
        at=priced_at,
        cache_write_5m=cache_write_5m,
        cache_write_1h=cache_write_1h,
        prices=catalog.prices,
    )
    if priced is not None:
        snapshot = _price_snapshot(priced)
    else:
        logger.warning(
            "[llm usage] model {model!r} is unpriced; add it to "
            "base/lm/pricing/pricing_catalog_archive.json or the plugin price registry",
            model=model,
        )
        snapshot = {"unpriced": 1}
    # The one figure set both the event and (via `record_out`) the message carry.
    record: dict[str, Any] = {
        "model": model,
        "in_total": in_total,
        "out_total": out_total,
        **cache_fields,
        "reasoning": reasoning,
        **snapshot,
    }
    if record_out is not None:
        record_out.update(record)

    vendor = vendor_of_model(model, catalog=catalog)
    if vendor is not None and emit_billing:
        emit_billing_event(
            vendor=vendor,
            model=model,
            tok_in=in_total,
            tok_out=out_total,
            tok_cached=cache_read,
            cache_write_5m=cache_write_5m,
            cache_write_1h=cache_write_1h,
            cost_usd=priced.cost_usd if priced is not None else 0.0,
            usage_kind=usage_kind,
            unpriced=priced is None,
            start_time_ns=(time.time_ns() - int(latency_ms * 1_000_000))
            if latency_ms is not None
            else None,
        )

    usage_logger = logger.bind(agent_id=for_agent_id) if for_agent_id is not None else logger
    usage_logger.info(
        "[llm usage] in={in_total}" + cache_text + "  "
        "out={out_total} reason={reasoning}{reason_pct}",
        event="llm_usage",
        calls=1,
        reason_pct=reason_pct,
        **({"requested_model": requested_model} if requested_model is not None else {}),
        latency_ms=latency_ms,
        decode_ms=decode_ms,
        usage_kind=usage_kind,
        **({"source": source, "transport_source": "system"} if source is not None else {}),
        **record,
    )
    return in_total + out_total, priced.cost_usd if priced is not None else 0.0


# ── billing spans ──

MEIVA_BILLING_ATTR_LINE = "meiva.billing.line"
MEIVA_BILLING_ATTR_VENDOR = "meiva.billing.vendor"
MEIVA_BILLING_ATTR_MODEL = "meiva.billing.model"
MEIVA_BILLING_ATTR_TOKENS_IN = "meiva.billing.tokens_in"
MEIVA_BILLING_ATTR_TOKENS_OUT = "meiva.billing.tokens_out"
MEIVA_BILLING_ATTR_COST = "meiva.billing.cost"
MEIVA_BILLING_ATTR_USAGE_KIND = "meiva.billing.usage_kind"
MEIVA_BILLING_ATTR_TS = "meiva.billing.ts"
MEIVA_BILLING_ATTR_CACHE_READ_TOKENS = "meiva.billing.cache_read_tokens"
MEIVA_BILLING_ATTR_CACHE_WRITE_5M_TOKENS = "meiva.billing.cache_write_5m_tokens"
MEIVA_BILLING_ATTR_CACHE_WRITE_1H_TOKENS = "meiva.billing.cache_write_1h_tokens"
MEIVA_BILLING_ATTR_UNPRICED = "meiva.billing.unpriced"

_CORE_VENDOR_PREFIXES: tuple[tuple[str, str], ...] = ()


def _is_qwen_family(model: str) -> bool:
    """Alibaba family ids nest directly after the stem (``qwen3.8-max``), so a
    bare ``qwen-`` prefix would miss them. Require a digit after the stem (or an
    exact ``qwen`` id) so a non-Alibaba ``qwenfoo-*`` id cannot be mis-attributed;
    plugin registration already rejects such prefixes, this is a second guard.
    """
    return model == "qwen" or (model.startswith("qwen") and len(model) > 4 and model[4].isdigit())


def vendor_of_model(model: str, *, catalog: ModelCatalog) -> str | None:
    """Return the registered manufacturer for ``model``, if one is known."""
    for prefix, vendor in _CORE_VENDOR_PREFIXES:
        if model.startswith(prefix):
            return vendor
    if _is_qwen_family(model):
        return "alibaba"

    for prefix, binding in catalog.bindings.items():
        if model.startswith(prefix):
            return binding.display_name.lower()
    for prefix, provider in catalog.decision_providers.items():
        if model.startswith(prefix):
            return provider.display_name.lower()
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
    line: str = "meiva",
    tok_cached: int | None = None,
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

        span = otel_trace.get_tracer("meiva.billing").start_span(
            "meiva.billing.call",
            start_time=start_time_ns,
        )
        span.set_attribute(MEIVA_BILLING_ATTR_LINE, line)
        span.set_attribute(MEIVA_BILLING_ATTR_VENDOR, vendor)
        span.set_attribute(MEIVA_BILLING_ATTR_MODEL, model)
        span.set_attribute(MEIVA_BILLING_ATTR_TOKENS_IN, int(tok_in))
        span.set_attribute(MEIVA_BILLING_ATTR_TOKENS_OUT, int(tok_out))
        span.set_attribute(MEIVA_BILLING_ATTR_COST, round(float(cost_usd), 6))
        span.set_attribute(MEIVA_BILLING_ATTR_USAGE_KIND, usage_kind)
        span.set_attribute(MEIVA_BILLING_ATTR_TS, _utc_timestamp())
        if tok_cached is not None:
            span.set_attribute(MEIVA_BILLING_ATTR_CACHE_READ_TOKENS, int(tok_cached))
        if cache_write_5m:
            span.set_attribute(MEIVA_BILLING_ATTR_CACHE_WRITE_5M_TOKENS, cache_write_5m)
        if cache_write_1h:
            span.set_attribute(MEIVA_BILLING_ATTR_CACHE_WRITE_1H_TOKENS, cache_write_1h)
        if unpriced:
            span.set_attribute(MEIVA_BILLING_ATTR_UNPRICED, unpriced)
        span.end()
    except Exception as exc:
        report_sink_failure("billing span emission (the span is dropped)", exc)


def emit_billing_from_message(
    msg: Any,
    *,
    model: str,
    usage_kind: str,
    line: str = "meiva",
    start_time_ns: int | None = None,
    vendor: str | None = None,
    catalog: ModelCatalog,
) -> None:
    """Price a LangChain message and emit its billing span when usage is known."""
    try:
        tok_in, tok_out, tok_cached = tally_tokens([msg])
        if tok_in is None or tok_out is None or tok_cached is None:
            return
        tok_cached = _metered_cache_read(msg.usage_metadata, tok_cached)
        model = usage_model(msg, model, catalog=catalog)
        resolved_vendor = vendor or vendor_of_model(model, catalog=catalog)
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
            prices=catalog.prices,
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
