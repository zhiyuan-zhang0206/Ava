"""LLM pricing — bench / eval both look up cost_usd here to prevent drift.

The rates themselves live on each model's registry entry
(`shared/lm/registry.py:MODELS`, the `pricing` fact); `MODEL_PRICING` below is
the derived per-model view. Sync with provider pricing:
- Anthropic: https://www.anthropic.com/pricing
- DeepSeek:  https://api-docs.deepseek.com/quick_start/pricing
- Google:    https://ai.google.dev/pricing
- OpenAI:    https://openai.com/api/pricing
- Xiaomi:    https://platform.xiaomimimo.com

## Cache hit vs miss must be computed separately

DeepSeek V4 Pro cache hit is 120x cheaper than cache miss; in
multi-turn re-send same prefix the actual cache hit rate is ~98%.
Using a 2-tuple (in, out) computes the entire `in` at cache-miss rate
and overestimates total cost by 30-50x — history: the same bug in
bench_runner.py + evals/driver.py (both in MyAva) reported $56.38 for a 30-case
SWE-bench, actual was $0.8; fix in PR #322 / commit 5c85b74. So the
pricing fact is a 3-tuple `(cache_miss, cache_hit, out)` USD/M.

## All cache pricing verified 2026-06-27 against provider pricing pages

Claude series: cache read = 0.1x input (anthropic.com/pricing).
DeepSeek Flash: cache hit = 50x discount (api-docs.deepseek.com).
Google Gemini: cache read ~0.1x input across all models (ai.google.dev/pricing).
OpenAI GPT-5.x: cached-input discount ~0.1x input.
DeepSeek Pro / MiMo: real cache-hit discount (~120x cheaper vs miss).
Kimi K2.7: real cache-hit discount ($0.19 vs $0.95 input).

## DeepSeek reasoning tokens

DeepSeek's reasoning tokens count into output — billed at the merged
output rate; no separation between reasoning vs answer
(usage_metadata reports separately but the underlying rate is the
same).
"""

from __future__ import annotations

from collections.abc import Sequence

from langchain_core.messages import AIMessage, AnyMessage

from shared.lm.registry import MODELS

# Derived view over the registry: `{model id: (cache_miss, cache_hit, out)}`
# USD/M for every model with a known rate. Rate provenance / caveats live as
# comments on the registry entries.
MODEL_PRICING: dict[str, tuple[float, float, float]] = {
    model_id: spec.pricing for model_id, spec in MODELS.items() if spec.pricing is not None
}

# Retired models — their FINAL published rate, frozen at retirement (add-only
# ledger). A model leaves MODELS when it is replaced (fail-fast on residual
# config_overlay references), but historical llm_usage rows keep their model
# id forever: cost is computed at usage time with the price in force then
# (user principle — price changes must never rewrite history), and the read
# path prices pre-snapshot legacy rows against THIS table (plus MODEL_PRICING)
# instead of the live registry, so a retirement does not drop historical rows
# from cost. Provenance per entry: the rate + the commit/date that retired it.
RETIRED_MODEL_PRICING: dict[str, tuple[float, float, float]] = {
    # gemini-3.6-flash — retired 2026-08-13 (swap to gemini-3.7-flash, GA;
    # commit 06c593a45). Final standard rate $1.50 / $7.50 per 1M, cache read
    # 0.1x input ($0.15) — the 3.7 entry carries the same post-intro rate.
    "gemini-3.6-flash": (1.5, 0.15, 7.5),
    # kimi-k2.7-code — removed from the registry 2026-08-04 (revert #850,
    # commits 630fe6b65/bc60f0151). Official ¥6.50 / ¥1.30 / ¥27.00 per 1M
    # converted at ~6.77 CNY/USD (the price the registry entry carried).
    "kimi-k2.7-code": (0.96, 0.19, 3.99),
}


def _rates_for(model: str) -> tuple[float, float, float] | None:
    """The rate in force for `model`: the live registry first, then the
    retired-model ledger. A model in neither has no known price."""
    rates = MODEL_PRICING.get(model)
    if rates is not None:
        return rates
    return RETIRED_MODEL_PRICING.get(model)


def tally_tokens(
    messages: Sequence[AnyMessage],
) -> tuple[int | None, int | None, int | None]:
    """Accumulate AIMessage.usage_metadata input/output/cache_read tokens.

    Returns `(in, out, cached)` triple. `in` includes `cached`
    (LangChain semantics: input_tokens is the total input;
    input_token_details.cache_read is the cache-hit portion;
    cache miss = in - cached).

    If no AIMessage has usage_metadata (extremely rare: mock LLM /
    old version), returns `(None, None, None)` instead of (0, 0, 0)
    — distinguishes "unknown" from "0 tokens"; cost_usd also returns
    None, so JSONL clearly shows data missing rather than a free call.
    """
    tok_in = 0
    tok_out = 0
    tok_cached = 0
    seen = False
    for m in messages:
        if not isinstance(m, AIMessage):
            continue
        meta = m.usage_metadata
        if not meta:
            continue
        seen = True
        tok_in += meta["input_tokens"]
        tok_out += meta["output_tokens"]
        details = meta.get("input_token_details") or {}
        tok_cached += int(details.get("cache_read", 0))
    if not seen:
        return None, None, None
    return tok_in, tok_out, tok_cached


def cost_usd(
    model: str,
    tok_in: int | None,
    tok_out: int | None,
    tok_cached: int | None = None,
) -> float | None:
    """Compute via 3-tier pricing:
    cost = miss*miss_rate + cache_hit*hit_rate + out*out_rate.

    When `tok_cached=None`, fall back to 0 (treats all as cache miss,
    overestimate but safe); for callers with only old `(in, out)`
    data. New callers should pass cache_read.

    Returns None when tokens are None (distinguishes "unknown" from
    "0 tokens") or the model has no known price (neither MODEL_PRICING nor
    the retired-model ledger — do not assume cost for an unlisted model).
    """
    if tok_in is None or tok_out is None:
        return None
    rates = _rates_for(model)
    if rates is None:
        return None
    miss_per_m, hit_per_m, out_per_m = rates
    cache_read = tok_cached or 0
    cache_miss = tok_in - cache_read
    return (cache_miss * miss_per_m + cache_read * hit_per_m + tok_out * out_per_m) / 1_000_000
