---
type: doc
title: Built-in plugins
description: Bundled plugin implementations and their component documentation.
tags: [extensions]
---

# Built-in plugins

These packages ship with Meiva. The generic registration, contribution surfaces,
loading and manifest contracts belong to [[docs/plugins/plugins.meiva.okf.md]].
Each implementation below owns its behavior and component documentation.

## DeepSeek provider plugin

The DeepSeek provider (`meiva_builtins/plugins/lm_deepseek/provider.py`) talks to DeepSeek's Anthropic-compatible endpoint via `DeepSeekChatModel`, reads `DEEPSEEK_API_KEY` from process environment or `.env` aliases (so gateway draft models work after capability-key stripping), maps graded reasoning effort onto `output_config.effort` via `extra_body` and `none` onto `thinking={"type": "disabled"}`, and declares native image attachment support (`media_types=frozenset({"image"})`, 32 MiB inline limit, 8192 px up to 14 images / 4096 px from 15 images) on `deepseek-flash` while keeping the prefix fallback `vision=False`.

`deepseek-flash` (DeepSeek V4.1-Flash) accepts Anthropic-format `image` blocks
on `/anthropic` (`document` blocks are unsupported, so PDF remains out), pins a
150s stream TTFT timeout to bound queue stalls, and uses 0.374 / 0.512
compaction thresholds on its 1M context window.

| Plugin | Responsibility |
|---|---|
| [[meiva_builtins/plugins/meiva_code/docs/meiva_code.meiva.okf.md]] | Coding workspace and context |
| [[meiva_builtins/plugins/meiva_fleet/docs/meiva_fleet.meiva.okf.md]] | Agent collaboration and tasks |
| [[meiva_builtins/plugins/meiva_memory/docs/meiva_memory.meiva.okf.md]] | Long-term memory |
| [[meiva_builtins/plugins/meiva_sdk_reminder/docs/meiva_sdk_reminder.meiva.okf.md]] | SDK usage reminders |
| [[meiva_builtins/plugins/meiva_silent_idle/docs/meiva_silent_idle.meiva.okf.md]] | Silent-turn continuation |
| [[meiva_builtins/plugins/meiva_syntax_fix/docs/meiva_syntax_fix.meiva.okf.md]] | Deterministic code corrections |
| [[meiva_builtins/plugins/meiva_to_user/docs/meiva_to_user.meiva.okf.md]] | To-user panel soft-limit reminder |

Skills and MCP packages have separate catalogs under Built-ins; plugin-owned
skills remain under their plugin's component tree.

On the `meiva.understand` media path, `media_resolution` maps `low`/`medium`/`high` onto `google.genai.types.MediaResolution` (imported lazily inside the provider branch) and `media_thinking_level` takes precedence over global `MEIVA_REASONING_EFFORT` while leaving `include_thoughts=None` so thought blocks are not requested. On the agent main path, `include_thoughts=True` surfaces canonical `{"type": "thinking", "thinking": ...}` blocks unless thinking is disabled. `gemini-3.8-flash`, `gemini-3.1-pro-preview`, and `gemini-4-argon` declare `effort_levels=("low", "medium", "high")` because `minimal` returns HTTP 400 on those models; `gemini-3.1-pro-preview` additionally pins `llm_retry_max_attempts=10` (preview rate limits) and `llm_stream_ttft_timeout_seconds=90.0` (Gemini streams no protocol preamble before the first thought/content chunk). `gemini-flash-lite-latest` tracks Google's stable Flash-Lite alias (`gemini-3.5-flash-lite`, defaulting to `minimal`).
