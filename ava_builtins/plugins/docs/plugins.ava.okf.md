---
type: doc
title: Built-in plugins
description: Bundled plugin implementations and their component documentation.
tags: [extensions]
---

# Built-in plugins

These packages ship with Ava. The generic registration, contribution surfaces,
loading and manifest contracts belong to [[docs/plugins/plugins.ava.okf.md]].
Each implementation below owns its behavior and component documentation.

## DeepSeek provider plugin

```ava-contract
id = "lm-deepseek-provider"
kind = "invariant"
statement = "The DeepSeek provider (`ava_builtins/plugins/lm_deepseek/provider.py`) talks to DeepSeek's Anthropic-compatible endpoint via `DeepSeekChatModel`, reads `DEEPSEEK_API_KEY` from process environment or `.env` aliases (so gateway draft models work after capability-key stripping), maps graded reasoning effort onto `output_config.effort` via `extra_body` and `none` onto `thinking={\"type\": \"disabled\"}`, and declares native image attachment support (`media_types=frozenset({\"image\"})`, 32 MiB inline limit, 8192 px up to 14 images / 4096 px from 15 images) on `deepseek-flash` while keeping the prefix fallback `vision=False`."
code = [
  "ava_builtins/plugins/lm_deepseek/provider.py:<module>",
  "ava_builtins/plugins/lm_deepseek/provider.py:deepseek_wire_effort",
  "ava_builtins/plugins/lm_deepseek/provider.py:_require_deepseek_key",
  "ava_builtins/plugins/lm_deepseek/provider.py:build",
  "ava_builtins/plugins/lm_deepseek/provider.py:PROVIDER",
]
tests = [
  "base/packages/tests/lm/test_lm_attach.py::test_deepseek_image_uses_a_data_uri_block",
  "base/packages/tests/lm/test_lm_attach.py::test_deepseek_attach_policy_switches_dimension_tier_at_image_15",
]
```

`deepseek-flash` (DeepSeek V4.1-Flash) accepts Anthropic-format `image` blocks
on `/anthropic` (`document` blocks are unsupported, so PDF remains out), pins a
150s stream TTFT timeout to bound queue stalls, and uses 0.374 / 0.512
compaction thresholds on its 1M context window.

| Plugin | Responsibility |
|---|---|
| [[ava_builtins/plugins/ava_code/docs/ava_code.ava.okf.md]] | Coding workspace and context |
| [[ava_builtins/plugins/ava_fleet/docs/ava_fleet.ava.okf.md]] | Agent collaboration and tasks |
| [[ava_builtins/plugins/ava_memory/docs/ava_memory.ava.okf.md]] | Long-term memory |
| [[ava_builtins/plugins/ava_sdk_reminder/docs/ava_sdk_reminder.ava.okf.md]] | SDK usage reminders |
| [[ava_builtins/plugins/ava_silent_idle/docs/ava_silent_idle.ava.okf.md]] | Silent-turn continuation |
| [[ava_builtins/plugins/ava_syntax_fix/docs/ava_syntax_fix.ava.okf.md]] | Deterministic code corrections |
| [[ava_builtins/plugins/ava_to_user/docs/ava_to_user.ava.okf.md]] | To-user panel soft-limit reminder |

Skills and MCP packages have separate catalogs under Built-ins; plugin-owned
skills remain under their plugin's component tree.

```ava-contract
id = "lm-google-provider"
kind = "invariant"
statement = "The Google Gemini provider (`ava_builtins/plugins/lm_google/provider.py`) constructs `ChatGoogleGenerativeAI` with `GEMINI_API_KEY` and passes `temperature=None` on every request so deprecated sampling parameters (`temperature`, `top_p`, `top_k`) are never sent, regardless of whether the model id is version-numbered (`gemini-4-argon`, `gemini-3.8-flash`) or an alias (`gemini-flash-lite-latest`). Deprecated `gemini-3.5-flash` and `gemini-3.7-flash` remain spawnable for existing agent configurations while declaring `superseded_by=\"gemini-3.8-flash\"` to hide them in the spawn picker and settle resurrected agents onto `gemini-3.8-flash`. Thinking depth maps through `thinking_level` (`media_thinking_level` on the media path, lowest declared rung plus `include_thoughts=False` when disabled on models that declare `effort_levels`, or `validate_effort` against the model's rungs), while models without `effort_levels` log a warning and omit thinking parameters when disabled. Promotional pricing periods activate price increases at the earliest global occurrence of the calendar date (UTC+14)."
code = ["ava_builtins/plugins/lm_google/provider.py:<module>"]
tests = [
  "ava_builtins/plugins/lm_google/tests/test_sampling_parameters.py::test_request_has_no_sampling_parameters",
  "ava_builtins/plugins/lm_google/tests/test_sampling_parameters.py::test_deprecated_flash_models_are_superseded_by_3_8_flash",
  "ava_builtins/plugins/lm_google/tests/test_implicit_cache.py::test_google_tool_call_uses_complete_prefix_and_accounts_cache_reads",
]
```

On the `ava.understand` media path, `media_resolution` maps `low`/`medium`/`high` onto `google.genai.types.MediaResolution` (imported lazily inside the provider branch) and `media_thinking_level` takes precedence over global `AVA_REASONING_EFFORT` while leaving `include_thoughts=None` so thought blocks are not requested. On the agent main path, `include_thoughts=True` surfaces canonical `{"type": "thinking", "thinking": ...}` blocks unless thinking is disabled. `gemini-3.8-flash`, `gemini-3.1-pro-preview`, and `gemini-4-argon` declare `effort_levels=("low", "medium", "high")` because `minimal` returns HTTP 400 on those models; `gemini-3.1-pro-preview` additionally pins `llm_retry_max_attempts=10` (preview rate limits) and `llm_stream_ttft_timeout_seconds=90.0` (Gemini streams no protocol preamble before the first thought/content chunk). `gemini-flash-lite-latest` tracks Google's stable Flash-Lite alias (`gemini-3.5-flash-lite`, defaulting to `minimal`).
