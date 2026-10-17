---
type: doc
title: Language model test contracts
description: Regression assertions for model construction, tuning and provider-error fixtures.
tags: [testing, llm-inference]
---

# Language model test contracts

These records bind regression tests to their current behavior owners. The
construction tests inspect client configuration without calling provider APIs.
They do not establish current vendor behavior.

## Billing evidence

Behavior owner: [[base/lm/docs/provider-errors.meiva.okf.md]].

Billing classification is independent of retry class. A specific billing code can coexist with a generic error type; reporting preserves the original type. A Google prepayment rejection remains distinguishable from ordinary rate limiting with the same HTTP status.

## Reasoning visibility

Behavior owner: [[base/lm/docs/lm/lm.meiva.okf.md]].

Adaptive Claude construction supplies summarized thinking display when absent and preserves an explicit display choice. OpenAI construction requests Responses API reasoning summaries. These settings keep reasoning text available to the timeline.

## Scaled budget

Behavior owner: [[base/lm/docs/lm/context-budget.meiva.okf.md]].

Context thresholds scale with the catalog window. DeepSeek Flash retains its model-specific 374k/512k thresholds. A binding hard ceiling scales the reminder by the same ratio so it remains below forced compaction.

## Provider key channel

Behavior owner: [[base/lm/docs/provider-plugins.meiva.okf.md]].

Provider key validation uses the declared environment channel, with the local environment file as fallback. Legacy Settings aliases do not authorize a spawn. Construction can use the fallback after a gateway profile removes a provider key from the process environment without restoring that key to the process.

## Explicit tuning

Behavior owner: [[base/lm/docs/model-configuration.meiva.okf.md]].

An explicit empty effort masks a model default. A profile without the owning settings domain resolves from the supplied model and shared layers without recovering a hidden settings reader.

## Response receipt fixtures

`providers/test_openai_fast.py` uses `Response.model_construct` to reproduce
the locked OpenAI SDK's non-strict network-response construction. Strict model
validation could reject a newer service-tier receipt before Meiva consumes it.
This fixture exercises receipt handling; it does not claim that strict parsing
accepts the same values or that a live provider returned them.
