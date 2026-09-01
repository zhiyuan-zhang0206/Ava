---
type: doc
title: Provider Metadata Schema v1 — Pricing Catalog
description: Cross-line provider metadata contract implemented by Ava's reviewed pricing catalog.
tags:
- shared
- lm
- billing
- schema
---

# Provider Metadata Schema v1 — Pricing Catalog

Source: authored by CTO #3230 (2026-09-01). Ava is the reference
implementation for this cross-line schema.

## Top-level fields

| Field | Type | Contract |
|---|---|---|
| `schema_version` | integer | `2` after this change. It identifies the pricing-catalog contract revision. |
| `catalog_version` | string | Version of this reviewed catalog snapshot. |
| `currency` | string | Always `"USD"`. |
| `unit_tokens` | integer | Always `1000000`; rates are USD per 1M tokens. |
| `models` | object | Non-empty mapping from a bare model name to a per-model entry. |

## Per-model fields

| Field | Type | Contract |
|---|---|---|
| `vendor` | string | Required from catalog schema v2. Registered open-enum examples: `openai`, `deepseek`, `gemini`, `anthropic`, `mimo`, `kimi`, `glm`, `qwen`, and additional registered providers. |
| `source_url` | string | HTTPS official pricing source. |
| `source_checked_at` | ISO date | Date the source was checked. |
| `effective_time_note` | optional string | Legacy note when a provider does not publish an exact effective time. |
| `periods` | non-empty array | Effective pricing periods. Every period carries `effective_from` and `effective_until` as ISO-8601 UTC timestamps or `null`, plus `tiers`. |
| `periods[].tiers` | non-empty array | Gapless input-token tiers with `input_tokens_min`, `input_tokens_max`, and `rates`. |
| `periods[].tiers[].rates` | object | String numbers `input`, `cache_read`, and `output`, in USD per 1M tokens. |
| `periods[].tiers[].utc_daily_overrides` | array | Optional UTC windows with `start` and `end` as `HH:MM:SS`, and replacement `rates`. |

## Model-key and vendor convention

The `models` object key is the bare model name. Vendor identity must never be
baked into a new key shape or recovered by parsing the key: `vendor` is the
authoritative, separately stored provider identity. Existing model keys remain
unchanged for compatibility.

## Evolution rules

The schema is only-add. New fields may be added when optional or supplied with
a default. Increment `schema_version` only for a breaking change, including a
new required field or a semantic change to an existing field. Existing field
names, units, and semantics remain stable across additive revisions.
