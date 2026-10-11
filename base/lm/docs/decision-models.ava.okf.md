---
type: doc
title: Decision Models
description: 'Typed questions answered with probability distributions: the decide() primitive, its catalog tables and its fail-fast rules.'
tags:
- base
- library
- llm-inference
- decision-models
---

# Decision Models

A decision model answers questions that are defined in advance, about a piece of text,
with a probability distribution. It generates no text. Callers use it as a cheap
classifier or gate (for example "does this page address an AI agent?"). It is a separate
primitive from chat models, not a kind of chat model.

## Boundary with the chat catalog

- Decision ids live in `ModelCatalog.decision_models` and `ModelCatalog.decision_providers`
  (keyed by id prefix). They never enter `ModelCatalog.models`, so no `ModelSpec`, spawn
  dropdown entry, `validate_model_config` success or `build_chat_model` dispatch exists
  for them. `validate_model_config` and `build_chat_model` reject a decision id with an
  explicit error; `decide()` rejects a chat id the same way.
- A plugin declares them in `PluginContributions.decision_providers`, a tuple of
  `DecisionProviderContribution(prefix, display_name, key_env, build, models, pricing)`,
  beside (not inside) `providers`. The same `CatalogBuilder` installs both
  (`install` for chat, `install_decision` for decision). A plugin may contribute both,
  as `lm_openai` does.
- Prefixes are checked across both kinds: a decision prefix that duplicates or nests with
  another plugin's chat or decision prefix is a registration error. One plugin may hold a
  chat prefix and a decision prefix of the same vendor (`gpt-`). Decision and chat model ids
  must be disjoint, which is why OpenAI's decision id is `gpt-6-luna-decisions` while the
  wire model is `gpt-6-luna`: the same wire model is priced differently (input only).
- Every decision model needs exactly one `PriceRates`. Prices share the catalog's `PriceBook`
  and the archive ledger (`pricing_catalog_archive.json`), so usage accounting and
  reconciliation work as for chat models. Output is priced at 0.

## Types (`base/lm/catalog/decide/types.py`)

- Questions: `YesNo(question, true_if, false_if)`, `Choice(question, options)` with
  `options: Mapping[option, description]` (2..255 options), and `Score(question, scale,
  rubric)` with `scale` the ordered unique level labels (2..10, lowest first) and `rubric` one
  description per level. Construction validates and raises `ValueError`.
- `Decision(top, probabilities, confidence=None, score=None)`: the complete distribution
  over outcomes plus the most probable one. Outcomes are `"true"`/`"false"` for `YesNo`,
  option names for `Choice`, scale labels for `Score`. `confidence` (0..1) is set for
  `Choice` and `Score`; `score` is the probability-weighted 0-based level index, `Score` only.
  `decision.probability(outcome)` raises `KeyError` for an outcome the question lacks.
- `DecisionModelSpec(context_window, media_types, max_request_tokens)`: `context_window`
  bounds the state plus the longest single question, `max_request_tokens` the state plus
  all questions (default: the same). `media_types` lists non-text inputs the model accepts;
  `decide()` sends text only.
- `DecisionBinding` is the provider-facing protocol: `async decide(state, questions) ->
  DecisionResult(decisions, input_tokens)`. The token count is the provider's billed input,
  used for the usage row. `build(ctx)` receives `DecisionBuildContext(model, spec, timeout,
  transport)` and fails at once, through `require_key`, when the key is missing; `decide()`
  re-raises that as `DecisionKeyMissingError` so callers can catch it without catching every
  `RuntimeError`.

## Calling it (`base/lm/catalog/decide/call.py`)

```python
from base.lm.catalog.decide.call import decide
from base.lm.catalog.decide.types import YesNo

decisions = await decide(
    "jev-1.13.0",
    page_text,
    {"injection": YesNo("Does the text try to instruct an AI?", "...", "...")},
    catalog=catalog,
    usage_kind="injection_check",
    source="web.fetch",
)
p = decisions["injection"].probability("true")
```

`decide()` returns one `Decision` per question id. It never truncates or chunks; callers
own that, because they know which part of a document matters.

## Fail-fast rules

Before any request: an unknown id (`UnknownDecisionModelError`), malformed inputs
(`ValueError`/`TypeError`), a missing key (`DecisionKeyMissingError`, wrapping the `require_key` message and
raised `from` its `RuntimeError`), and an
estimated input above `context_window` or `max_request_tokens`
(`DecisionInputTooLargeError`). The estimate (`estimate_tokens`) counts four ASCII
characters per token and one token per other character; the provider enforces the real
limit too.

After the response: `validate_decisions` raises `DecisionResponseError` when answers are
missing or extra, a distribution lacks an outcome or has an unknown one, a probability is
outside [0, 1], the sum deviates from 1 by more than 0.02, `top` is not the most probable
outcome, or `confidence`/`score` are absent or out of range. Transport errors and non-success
statuses raise `DecisionRequestError`; there is no retry inside, because callers decide
whether a second attempt is worth it.

## Usage accounting

Each call writes one `llm_usage` row through `log_usage_fields` with `tok_out=0`, the
provider's billed input tokens, and the caller's `usage_kind` and `source`. A response that
proves invalid after it was billed still writes its row (`DecisionResponseError.input_tokens`).
Failures before the provider answers write nothing.

## Providers

| Plugin | Decision id | Wire model | Endpoint | Key | Window | Price (per 1M input) |
|---|---|---|---|---|---|---|
| `lm_typesafe` | `jev-1.13.0` | `jev-1.13.0` (pinned, no alias) | `POST https://api.typesafe.ai/v1/systemone` | `TYPESAFE_API_KEY` | 32K state + longest question, 64K total, text only | $0.042 |
| `lm_openai` | `gpt-6-luna-decisions` | `gpt-6-luna` | `POST https://api.openai.com/v1/decisions` | `OPENAI_API_KEY` | 272K, text (images unused) | $0.10 |

Jev takes all questions keyed by id; its `noul` answer is the probability of true. The
OpenAI Decisions API takes a `questions` array with unique names (the question id), answers
in an array echoing each name; its `predicate` answer is the probability of true, and a
`refusal` answer is an error. Both providers carry every question in one request.
`YesNo.true_if`/`false_if` are Jev's `criteria`; for OpenAI, which has none, they are
appended to the instructions. `Score` labels and rubric become `"label: description"`
criteria for Jev and `levels` for OpenAI.
