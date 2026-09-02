---
type: doc
title: LLM Billing Catalog
description: '`shared/lm/pricing.py` + `pricing_catalog.json` — the reviewed, network-free price source behind every cost figure.'
tags:
- shared
- library
- llm-inference
- billing
---

# LLM Billing Catalog

`shared/lm/pricing.py` + `pricing_catalog.json` — how one call becomes one dollar figure. Split out of [[lm.ava.okf.md]] when the provider overview hit its size cap; the selection rules and the per-vendor sourcing traps had outgrown a four-bullet section.

## Selection

- The reviewed JSON catalog is the sole volatile pricing source: every model has official-source provenance (`source_url` + `source_checked_at`) plus gapless effective periods, input-token tiers, and optional recurring UTC rate windows. The registry carries no duplicate price tuples.
- Every catalog entry carries a schema-v2 `vendor`; its separation from the bare model key matches the cross-line billing-event schema ([`pricing_catalog_schema.md`](pricing_catalog_schema.md)).
- `rates_at(model, at, input_tokens)` selects one exact 3-rate tuple `(cache_miss, cache_hit, out)` USD/M. `quote()` returns those rates and the computed cost atomically, so a scheduled boundary cannot split the event snapshot; `cost_usd()` remains the compatibility reader and returns `None` for unknown models.
- Cache-hit and cache-miss input are priced separately on purpose — a 2-tuple once overestimated a 30-case batch by ~70x ($56.38 against $0.8).
- Date-only future increases with no provider timezone use the documented conservative UTC+14 boundary and carry an `effective_time_note`; exact published instants are used unchanged.
- Retired models keep their final rate in `RETIRED_MODEL_PRICING`, an add-only ledger: cost is computed at usage time with the price in force then, so a retirement never drops historical rows and a price change never rewrites history.

## Sourcing

- `scripts/update_model_pricing.py` reconciles strict official-source adapters with the checked-in catalog. Automation proposes an append-only, reviewable PR; runtime pricing stays deterministic and network-free. Only DeepSeek has an adapter today — every other vendor is a reviewed manual edit.
- **Never convert a vendor's CNY prices.** Alibaba publishes USD per model rather than converting at one rate: qwen3.8-max implies ~7.27 CNY/USD, qwen3.8-27b ~7.08. Deriving 27b at max's rate gives 0.4125/1.65/0.0825 against the published 0.424/1.696/0.085 — a ~3% error baked into every cost row. Read the model page's own USD column.
- **A tier boundary must be an exact token count**, and the parser validates gapless coverage. Alibaba's docs express Qwen boundaries only as `Input<=256k`, so a tiered Qwen cannot be registered off the docs website; an account's own `GET /api/v1/models` reports `"range_name": "Default"` (single flat tier) and is the authority on tiering. All three registered Qwen models are flat.
- **qwen3.8-flash uses the landed Model Studio EN page's Beijing USD column** (`$0.113 / $0.014 / $0.382`, checked 2026-09-02). The earlier QwenCloud figures tracked the Singapore column, overstated Beijing cost, and are retired (see the module docstring).
- Rates are **region-specific**, and Qwen's endpoint is configurable (`AVA_DASHSCOPE_BASE_URL`). The catalog's Qwen entries are Beijing; Singapore prices the same models differently (qwen3.8-max $2.00/$0.25/$6.00 against Beijing's $1.65/$0.206/$4.951). Repointing the base URL at another region means re-checking this catalog.

## Notes

- **DeepSeek reasoning tokens** count into output and bill at the merged output rate; `usage_metadata` reports them separately but the underlying rate is the same.
- Providers reporting a cache hit through the OpenAI `prompt_tokens_details.cached_tokens` field reach `cache_read` with no per-vendor wiring — langchain-openai maps it, `tally_tokens` sums it, `quote()` prices it.

- Key deps: [[lm.ava.okf.md]] (the provider overview this split out of)
