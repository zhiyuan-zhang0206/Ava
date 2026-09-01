"""Observability utilities — LLM usage observation.

`log_llm_usage(msg)` extracts standardized token usage from LangChain
`usage_metadata` and logs it.

(The old `section()` wrapper has been removed — graph nodes now directly
call `logger.info("[label] ...")` to log phase markers; the `[bracket]`
prefix is kept as a convention for grep-friendliness.)
"""

import time
from datetime import datetime

from langchain_core.messages import AIMessage

from shared.log import logger


def log_llm_usage(
    msg: AIMessage,
    model: str,
    *,
    latency_ms: float | None = None,
    decode_ms: float | None = None,
    priced_at: datetime | None = None,
    task_id: int | None = None,
) -> tuple[int, float] | None:
    """Log LangChain standardized usage fields from `usage_metadata`.

    Consistent across providers — Anthropic (via ThinkingTokensChatAnthropic),
    OpenAI, and Gemini all plumb cache + reasoning numbers here.
    `response_metadata.token_usage` is inconsistent across providers on the
    streaming accumulation path; using `usage_metadata` (LC standardized
    fields) is more reliable.

    Logs 4 numbers:
        in        total input tokens (including cache_read)
        cached    cache-hit portion of input
        out       total output tokens (including reasoning)
        reason    model reasoning portion of output (Anthropic thinking_tokens
                  mapped to output_token_details.reasoning via
                  ThinkingTokensChatAnthropic; OpenAI reasoning maps to
                  output_token_details.reasoning_tokens — both are read)

    The payload also carries the **usage-time price snapshot** — `cost_usd`
    (the call's USD cost) plus the three per-1M rates used
    (`price_miss`/`price_hit`/`price_out`) — computed here via
    `shared.lm.pricing.quote` with the catalog price in force at the call.
    Cost is billed incrementally at usage time (user principle): the
    read side sums the stored snapshot and never re-prices against the
    current catalog, so a later price or model change rewrites no history.
    Rows of a model with no known price carry no snapshot fields (absent =
    unpriced, counted as `unpriced_calls` by the readers).

    `latency_ms` is the call's wall-clock from request start to message
    completion (measured in `agent.graph._llm._stream_with_cache_retry`,
    covering the stale-cache retry and the non-streaming fallback), recorded
    so the ops monitor panel can render per-bucket p50/p95/max latency and
    tokens-per-second throughput (see `gateway/ops_series_lgtm.py`). Rows before
    this field existed carry NULL — percentile queries ignore them.

    `decode_ms` is the streaming decode window — last chunk arrival minus
    first chunk arrival, also measured in `_stream_with_cache_retry` via
    `_consume_stream_with_stall_timeout` (monotonic, ms). It excludes
    network / queue / prefill that latency_ms still carries, so
    Σ(out_total)/Σ(decode_ms) is the pure generation-stage output TPS.
    Non-streaming fallback calls and empty streams carry NULL (no honest
    window — never a fake number); the ops panel guards with
    `attributes ? 'decode_ms'` and buckets stay blank until the
    2026-08-04 instrumentation ships.

    `priced_at` is an optional timezone-aware instant for deterministic replay
    and boundary tests. Production callers omit it and select the current UTC
    catalog interval exactly once inside `quote()`.

    `task_id` is an explicit turn-level attribution. It is omitted for all
    untagged usage; an agent owning a task is not enough to imply one. It
    feeds task-budget counters through the `llm_usage` event only: the
    provider-call billing span is deliberately task-agnostic, because that
    ledger contract has no task dimension.

    Post single-tool execute_code refactor, the core metric is `reason`
    monotonically decreasing across turns — the previous turn's thinking
    blocks transparently echo into the next turn's input (server verifies
    signature, cache hits, no need to re-reason), so the model does not
    re-plan. Turn 1 has long reasoning; in subsequent turns `reason` should
    drop to tens of tokens (DeepSeek-v4 probe measured; Anthropic path same
    mechanism different implementation, trend still holds).
    """
    um = msg.usage_metadata
    if not um:
        return None
    in_total = um.get("input_tokens", 0) or 0
    out_total = um.get("output_tokens", 0) or 0
    cache_read = (um.get("input_token_details") or {}).get("cache_read", 0) or 0
    from shared.lm.reasoning import extract_reasoning_tokens

    reasoning = extract_reasoning_tokens(um, content=msg.content)  # pyright: ignore[reportUnknownArgumentType, reportUnknownMemberType]
    cache_pct = f" ({cache_read / in_total * 100:.0f}%)" if in_total else ""
    reason_pct = f" ({reasoning / out_total * 100:.0f}%)" if out_total else ""
    # Usage-time price snapshot (user principle: cost is billed at usage
    # time, never re-priced against the current catalog). Unpriced models
    # emit no snapshot fields — absent means unpriced, and the read side
    # counts those calls as unpriced instead of billing them at 0.
    from shared.lm.pricing import quote

    priced = quote(model, in_total, out_total, cache_read, at=priced_at)
    if priced is not None:
        snapshot = {
            "cost_usd": priced.cost_usd,
            "price_miss": priced.rates.cache_miss,
            "price_hit": priced.rates.cache_hit,
            "price_out": priced.rates.output,
        }
    else:
        # unpriced=1 makes unpriced call volume countable in Prometheus
        # (ava_llm_usage_unpriced_total); absent on priced calls.
        logger.warning(
            "[llm usage] model {model!r} is unpriced; add it to "
            "shared/lm/pricing_catalog.json or the plugin price registry",
            model=model,
        )
        snapshot = {"unpriced": 1}
    from shared.lm.billing import emit_billing_event, vendor_of_model

    vendor = vendor_of_model(model)
    if vendor is not None and "input_tokens" in um and "output_tokens" in um:
        emit_billing_event(
            vendor=vendor,
            model=model,
            tok_in=in_total,
            tok_out=out_total,
            tok_cached=cache_read,
            cost_usd=priced.cost_usd if priced is not None else 0.0,
            usage_kind="agent",
            unpriced=priced is None,
            start_time_ns=(time.time_ns() - int(latency_ms * 1_000_000))
            if latency_ms is not None
            else None,
        )
    logger.info(
        "[llm usage] in={in_total} cached={cache_read}{cache_pct}  "
        "out={out_total} reason={reasoning}{reason_pct}",
        # event= makes PostgresSink classify this line as
        # `events.event_name='llm_usage'`; `/api/stats/dashboard` windowed
        # token aggregation filters by the event field.
        event="llm_usage",
        # calls=1: the OTLP mapping turns this into ava_llm_usage_calls_total,
        # the per-agent/per-model call counter (histogram counts drop agent_id).
        calls=1,
        in_total=in_total,
        cache_read=cache_read,
        cache_pct=cache_pct,
        out_total=out_total,
        reasoning=reasoning,
        reason_pct=reason_pct,
        # model lands in the jsonb payload (not referenced in the format
        # string); the read side groups cost by model id as written.
        model=model,
        # latency_ms also rides the payload (None -> JSON null -> SQL NULL);
        # the format string never mentions it.
        latency_ms=latency_ms,
        # decode_ms rides the payload the same way — the generation-stage TPS
        # panel sums it per bucket; absent on rows before the 2026-08-04
        # instrumentation (attributes ? 'decode_ms' keeps them out).
        decode_ms=decode_ms,
        **({"task_id": task_id} if task_id is not None else {}),
        # the usage-time price snapshot — only when the model is priced.
        **snapshot,
    )
    return in_total + out_total, priced.cost_usd if priced is not None else 0.0
