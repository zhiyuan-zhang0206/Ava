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

Behavior owner: [[base/lm/docs/provider-errors.ava.okf.md]].

```ava-contract
id = "billing-evidence"
kind = "invariant"
statement = """Billing classification is independent of retry class. A specific billing code can coexist with a generic error type; reporting preserves the original type. A Google prepayment rejection remains distinguishable from ordinary rate limiting with the same HTTP status."""
code = ["base/packages/tests/lm/providers/test_provider_errors.py:test_dashscope_billing_codes_are_billing", "base/packages/tests/lm/providers/test_provider_errors.py:test_billing_code_match_does_not_widen_the_reported_error_type", "base/packages/tests/lm/providers/test_provider_errors.py:test_google_genai_prepay_depletion_is_billing", "base/packages/tests/lm/providers/test_provider_errors.py:test_google_genai_rate_limit_429_is_not_billing"]
tests = ["base/packages/tests/lm/providers/test_provider_errors.py::test_dashscope_billing_codes_are_billing", "base/packages/tests/lm/providers/test_provider_errors.py::test_billing_code_match_does_not_widen_the_reported_error_type", "base/packages/tests/lm/providers/test_provider_errors.py::test_google_genai_prepay_depletion_is_billing", "base/packages/tests/lm/providers/test_provider_errors.py::test_google_genai_rate_limit_429_is_not_billing"]
```

## Reasoning visibility

Behavior owner: [[base/lm/docs/lm/lm.ava.okf.md]].

```ava-contract
id = "reasoning-visibility"
kind = "invariant"
statement = """Adaptive Claude construction supplies summarized thinking display when absent and preserves an explicit display choice. OpenAI construction requests Responses API reasoning summaries. These settings keep reasoning text available to the timeline."""
code = ["base/packages/tests/lm/llm_factory/test_reasoning_effort_dispatch.py:TestReasoningEffortDispatch.test_adaptive_claude_defaults_to_summarized_display", "base/packages/tests/lm/llm_factory/test_reasoning_effort_dispatch.py:TestReasoningEffortDispatch.test_adaptive_caller_display_respected", "base/packages/tests/lm/llm_factory/test_provider_construction.py:TestProviderConstruction.test_gpt_enables_responses_api_reasoning_summary"]
tests = ["base/packages/tests/lm/llm_factory/test_reasoning_effort_dispatch.py::TestReasoningEffortDispatch::test_adaptive_claude_defaults_to_summarized_display", "base/packages/tests/lm/llm_factory/test_reasoning_effort_dispatch.py::TestReasoningEffortDispatch::test_adaptive_caller_display_respected", "base/packages/tests/lm/llm_factory/test_provider_construction.py::TestProviderConstruction::test_gpt_enables_responses_api_reasoning_summary"]
```

## Scaled budget

Behavior owner: [[base/lm/docs/lm/context-budget.ava.okf.md]].

```ava-contract
id = "scaled-budget"
kind = "invariant"
statement = """Context thresholds scale with the catalog window. DeepSeek Flash retains its model-specific 374k/512k thresholds. A binding hard ceiling scales the reminder by the same ratio so it remains below forced compaction."""
code = ["base/packages/tests/lm/test_context_budget.py:test_deepseek_budget_is_374k_soft_512k_hard", "base/packages/tests/lm/test_context_budget.py:test_ceiling_caps_the_hard_threshold"]
tests = ["base/packages/tests/lm/test_context_budget.py::test_deepseek_budget_is_374k_soft_512k_hard", "base/packages/tests/lm/test_context_budget.py::test_ceiling_caps_the_hard_threshold"]
```

## Provider key channel

Behavior owner: [[base/lm/docs/provider-plugins.ava.okf.md]].

```ava-contract
id = "provider-key-channel"
kind = "invariant"
statement = """Provider key validation uses the declared environment channel, with the local environment file as fallback. Legacy Settings aliases do not authorize a spawn. Construction can use the fallback after a gateway profile removes a provider key from the process environment without restoring that key to the process."""
code = ["base/packages/tests/lm/test_lm_factory.py:test_plugin_key_ignores_legacy_settings_field", "base/packages/tests/lm/llm_factory/test_provider_construction.py:TestProviderConstruction.test_deepseek_gateway_popped_key_falls_back_to_env_file"]
tests = ["base/packages/tests/lm/test_lm_factory.py::test_plugin_key_ignores_legacy_settings_field", "base/packages/tests/lm/llm_factory/test_provider_construction.py::TestProviderConstruction::test_deepseek_gateway_popped_key_falls_back_to_env_file"]
```

## Explicit tuning

Behavior owner: [[base/lm/docs/model-configuration.ava.okf.md]].

```ava-contract
id = "explicit-tuning"
kind = "invariant"
statement = """An explicit empty effort masks a model default. A profile without the owning settings domain resolves from the supplied model and shared layers without recovering a hidden settings reader."""
code = ["base/packages/tests/lm/registry/test_settings.py:test_explicit_empty_string_beats_per_model_effort", "base/packages/tests/lm/registry/test_settings.py:test_resolve_setting_degrades_when_owning_domain_not_in_profile"]
tests = ["base/packages/tests/lm/registry/test_settings.py::test_explicit_empty_string_beats_per_model_effort", "base/packages/tests/lm/registry/test_settings.py::test_resolve_setting_degrades_when_owning_domain_not_in_profile"]
```
