"""Canonical durable usage accounting for completed LLM calls."""

from __future__ import annotations

import time
from datetime import datetime

from langchain_core.messages import AIMessage

from shared.log import logger


def log_usage_from_message(
    msg: AIMessage,
    model: str,
    *,
    latency_ms: float | None = None,
    decode_ms: float | None = None,
    priced_at: datetime | None = None,
    task_id: int | None = None,
    usage_kind: str = "agent",
    source: str | None = None,
    for_agent_id: int | None = None,
) -> tuple[int, float] | None:
    """Log one completed LangChain message's token usage and price snapshot."""
    from shared.lm.pricing import tally_tokens

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
    from shared.lm.reasoning import extract_reasoning_tokens

    reasoning = extract_reasoning_tokens(msg.usage_metadata, content=msg.content)  # pyright: ignore[reportUnknownArgumentType, reportUnknownMemberType]
    return _log_usage(
        model,
        in_total=in_total,
        out_total=out_total,
        cache_read=cache_read,
        reasoning=reasoning,
        latency_ms=latency_ms,
        decode_ms=decode_ms,
        priced_at=priced_at,
        task_id=task_id,
        usage_kind=usage_kind,
        source=source,
        for_agent_id=for_agent_id,
        emit_billing="input_tokens" in usage_metadata and "output_tokens" in usage_metadata,
    )


def log_usage_fields(
    model: str,
    *,
    tok_in: int,
    tok_out: int,
    tok_cached: int = 0,
    tok_reasoning: int = 0,
    latency_ms: float | None = None,
    usage_kind: str,
    for_agent_id: int | None = None,
) -> tuple[int, float]:
    """Log raw provider token counts when no LangChain message exists."""
    return _log_usage(
        model,
        in_total=tok_in,
        out_total=tok_out,
        cache_read=tok_cached,
        reasoning=tok_reasoning,
        latency_ms=latency_ms,
        usage_kind=usage_kind,
        for_agent_id=for_agent_id,
    )


def _log_usage(
    model: str,
    *,
    in_total: int,
    out_total: int,
    cache_read: int,
    reasoning: int,
    latency_ms: float | None,
    usage_kind: str,
    decode_ms: float | None = None,
    priced_at: datetime | None = None,
    task_id: int | None = None,
    source: str | None = None,
    for_agent_id: int | None = None,
    emit_billing: bool = True,
) -> tuple[int, float]:
    """Emit one priced or explicitly unpriced usage event and billing span."""
    from shared.lm.billing import emit_billing_event, vendor_of_model
    from shared.lm.pricing import quote

    cache_pct = f" ({cache_read / in_total * 100:.0f}%)" if in_total else ""
    reason_pct = f" ({reasoning / out_total * 100:.0f}%)" if out_total else ""
    priced = quote(model, in_total, out_total, cache_read, at=priced_at)
    if priced is not None:
        snapshot = {
            "cost_usd": priced.cost_usd,
            "price_miss": priced.rates.cache_miss,
            "price_hit": priced.rates.cache_hit,
            "price_out": priced.rates.output,
        }
    else:
        logger.warning(
            "[llm usage] model {model!r} is unpriced; add it to "
            "shared/lm/pricing_catalog_archive.json or the plugin price registry",
            model=model,
        )
        snapshot = {"unpriced": 1}

    vendor = vendor_of_model(model)
    if vendor is not None and emit_billing:
        emit_billing_event(
            vendor=vendor,
            model=model,
            tok_in=in_total,
            tok_out=out_total,
            tok_cached=cache_read,
            cost_usd=priced.cost_usd if priced is not None else 0.0,
            usage_kind=usage_kind,
            unpriced=priced is None,
            start_time_ns=(time.time_ns() - int(latency_ms * 1_000_000))
            if latency_ms is not None
            else None,
        )

    usage_logger = logger.bind(agent_id=for_agent_id) if for_agent_id is not None else logger
    usage_logger.info(
        "[llm usage] in={in_total} cached={cache_read}{cache_pct}  "
        "out={out_total} reason={reasoning}{reason_pct}",
        event="llm_usage",
        calls=1,
        in_total=in_total,
        cache_read=cache_read,
        cache_pct=cache_pct,
        out_total=out_total,
        reasoning=reasoning,
        reason_pct=reason_pct,
        model=model,
        latency_ms=latency_ms,
        decode_ms=decode_ms,
        usage_kind=usage_kind,
        **({"task_id": task_id} if task_id is not None else {}),
        **({"source": source, "transport_source": "system"} if source is not None else {}),
        **snapshot,
    )
    return in_total + out_total, priced.cost_usd if priced is not None else 0.0
