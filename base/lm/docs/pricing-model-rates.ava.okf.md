---
type: doc
title: Published Model Rates
description: Reviewed model-specific token rates, cache-write gaps, and retirement caveats in Ava's pricing catalog.
tags:
- base
- library
- llm-inference
- billing
---

# Published Model Rates

The provider plugins and `pricing_catalog_archive.json` hold these reviewed rates.

## Fast inference services

Fast service IDs have independently declared prices, even when their wire model
is shared with Standard. The following USD per million token rates were checked
2026-10-07 against [OpenAI pricing](https://developers.openai.com/api/docs/pricing)
and [Anthropic pricing](https://platform.claude.com/docs/en/about-claude/pricing).
Triples are input / cache read / output. OpenAI's long-context rate prices the
whole request when input exceeds 272,000 tokens.

| Ava service ID | Base rate | Long-context rate |
| --- | --- | --- |
| `gpt-6-astra-fast` | 20 / 2 / 100 | 40 / 4 / 150 |
| `gpt-6.1-sol-fast` | 4 / 0.20 / 20 | 8 / 0.40 / 30 |
| `gpt-6-sol-fast` | 4 / 0.40 / 20 | 8 / 0.80 / 30 |
| `gpt-6-luna-fast` | 0.20 / 0.02 / 1 | 0.40 / 0.04 / 1.50 |
| `gpt-5.6-sol-fast` | 8 / 0.80 / 40 | 16 / 1.60 / 60 |
| `gpt-5.6-terra-fast` | 4 / 0.40 / 24 | 8 / 0.80 / 36 |
| `gpt-5.6-luna-fast` | 0.40 / 0.04 / 2.40 | 0.80 / 0.08 / 3.60 |
| `claude-opus-5-fast` | 10 / 1 / 50 | Same |
| `claude-opus-5-5-fast` | 8 / 0.40 / 40 | Same |

The actual response service receipt controls billing. OpenAI may downgrade a
Fast request to Standard; missing or unknown Fast receipts fail rather than
guessing its price. Claude Fast remains subject to vendor access approval.
Anthropic cache writes are still outside Ava's three-rate accounting model.

## Published model rates

- **gpt-5.6-sol carries its promotional price** ($4 in / $0.4 cached / $20 out per 1M, official model page checked 2026-09-06, valid at least through 2026-11-21). The revert to the standard rates ($5 / $0.5 / $30) is a deliberate manual flip in the plugin + archive, not an automatic period boundary (405 ruling 2026-09-07).
- **claude-opus-5-5** costs $4 input / $0.20 cache read / $20 output per 1M tokens ([Anthropic pricing](https://www.anthropic.com/pricing), checked 2026-09-25). Cache writes cost $5 for 5m and $8 for 1h per 1M; Ava bills the reported write TTL separately from ordinary input. `claude-opus-5` remains spawnable but is display-superseded by Opus 5.5.
- **claude-sonnet-5-5** costs $2 input / $0.20 cache read / $10 output per 1M tokens ([official model page](https://platform.claude.com/docs/en/models/sonnet-5-5/overview), checked 2026-09-30). Cache writes cost $2.50 for 5m and $4 for 1h per 1M; Ava bills the reported write TTL separately from ordinary input. `claude-sonnet-5` remains spawnable but is display-superseded.
- **gpt-6-sol** costs $2 input / $0.20 cached / $10 output per 1M for up to 272K input tokens, then $4 / $0.40 / $15 for the full request ([official model page](https://developers.openai.com/api/docs/models/gpt-6-sol), checked 2026-09-25). Cache writes cost $2.50 per 1M and are not modeled. `gpt-5.6-sol` and `gpt-6-sol` remain spawnable but are display-superseded by their next Sol model.
- **gpt-6.1-sol** costs $2 input / $0.10 cached / $10 output per 1M for up to 272K input tokens, then $4 / $0.20 / $15 for the full request ([official model page](https://developers.openai.com/api/docs/models/gpt-6.1-sol), checked 2026-09-30). Cache writes cost $2.50 per 1M and are not modeled.
- **gpt-6-luna** costs $0.10 input / $0.01 cached / $0.50 output per 1M for up to 272K input tokens, then $0.20 / $0.02 / $0.75 for the full request ([official model page](https://developers.openai.com/api/docs/models/gpt-6-luna), checked 2026-09-25). Cache writes cost $0.125 per 1M and are not modeled. `gpt-5.6-luna` remains spawnable and is display-superseded.
- **DeepSeek's 2026-09-10 V4.1-Flash release cut Flash prices to `$0.003/$0.006` cache hit, `$0.15/$0.30` cache miss, `$0.60/$1.20` output (off-peak/peak)** from 2026-09-09T16:00:00Z — the Beijing-midnight boundary for the Change Log date the page publishes in place of an instant. The retired `deepseek-v4-pro`, `deepseek-v4-flash`, and `deepseek-v4-flash-vision-exp` ids are absent from the registry; their historical prices remain in the archive. V4 Pro routed to V4.1 Flash from 2026-09-14T04:00:00Z; the archive records that succession as the entry's final period. Name only `deepseek-flash` in configurations; a removed id fails spawn validation as unknown. The page scopes peak hours Monday-Friday while the archive's UTC windows recur daily, so weekends bill at peak rates — a known overestimate.
- **Xiaomi's MiMo V2.6 launch prices** are `$0.435/$0.0036/$0.87` for `mimo-v2.6-pro` and `$4.35/$0.036/$8.70` for `mimo-v2.6-pro-ultraspeed` (cache miss/cache hit/output USD per 1M; [official pricing](https://mimo.mi.com/docs/price/pay-as-you-go), checked 2026-09-22). `mimo-v2.5-pro` stays config-valid at identical rates but is display-superseded by V2.6 Pro. The removed `mimo-v2.5-pro-ultraspeed` id keeps historical prices in the archive; a config naming it fails spawn validation as unknown and must name a current id.
