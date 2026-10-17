---
type: doc
title: Effective Agent Model Configuration
description: Shared model and effort validation for birth, fork, launch and restart.
tags:
- base
- llm-inference
---

# Effective Agent Model Configuration

`base/agents/model/birth_config.py:resolve_default_model` resolves the DB-backed
cluster default, otherwise the current config default, through the model
catalog's declared availability fallback. The default-model API, model picker,
birth stamp and spawn preflight share this owner. A fork preserves its inherited
birth stamp rather than taking the new cluster default.

`model_config.py` validates the final agent configuration: spawn overlays over
current birth defaults (or a fork's inherited stamp), and restart edits over
the stored overlay and birth stamp. Ordinary, guarded and self restart check
model/effort changes under the row lock before persisting configuration or a
restart command. Config edits skip credential checks; launch validates keys
against its saved birth configuration. The birth transaction rechecks its
actual resolved model and effort before commit.

`validate_restart_model_config` checks only edits to `llm_model` or
`reasoning_effort`. It merges the proposed overlay over the stored overlay and
birth stamp while holding the metadata row lock. Legacy rows without a pinned
model use the caller's explicit current default; pinned rows retain their
model. Unsupported model/effort combinations raise `InvalidModelConfig` before
configuration or restart writes. Guarded receipt replay precedes validation.

The factory's `check_provider_key=False` skips credentials only; model membership
and exact effort validation still apply. Self restart commits the config edit
and restart inbound together before raising `AgentRestart`.

Catalog membership belongs to admission, using the corresponding Installation or
request owner. `RestartAgentRequest` validates only structure and numeric/effort
ranges. Gateway admission retains its 422 refusal before forwarding; runner
membership and provider validation retain typed `InvalidModelConfig` 400 errors
before entering the write transaction. Guarded native receipt replay precedes
fresh validation, preserving the original accepted command. SDK and native
acceptance pass catalog, override and current-default inputs explicitly.

Tuning resolution receives explicit values from its caller. A display caller in
a profile without the owning settings domain supplies no explicit value and
resolves the model layer over the shared floor. An explicitly empty effort is a
real override selecting provider defaults. Agent override slices also feed
context budgets and model construction; display and runtime must not resolve
different values for the same supplied layers.

Per-model-defaultable settings fields use `None` as their default rather than inspecting Pydantic's `model_fields_set` because a split agent-runner receives `/api/bootstrap` values (defaults included) in `os.environ`, which would mark every field explicit on a runner; the `None` sentinel travels uniformly as data (`bootstrap_config_values` skips `None`). In `DEFAULT_TUNING`, `auto_compact_fraction=0.4` and `compact_reminder_fraction=0.3` scale with `context_window` (`auto_compact_ceiling_tokens=0` disables an absolute cap), `llm_stream_inter_chunk_timeout_seconds=300.0` tolerates normal mid-stream silence during omitted thinking or tool-call generation while `llm_stream_total_timeout_seconds=3600.0` bounds drip-feeding streams, `agent_communication_style="off"` omits narration guidance unless explicitly enabled, and `prompt_user_tone_enabled=True` applies by default while Claude models opt out. On `ModelSpec`, `max_output_tokens` is required on Anthropic-protocol bindings (`claude`/`deepseek`) because `langchain-anthropic` otherwise falls back to 4096 and truncates thinking mid-turn; `extended_thinking_only` marks Claude models that accept only `budget_tokens` and reject `effort`; `thinking_always_on` marks models whose reasoning cannot be disabled on the wire (`kimi-k3`, `glm-5.3*`); `superseded_by` hides a still-spawnable model in the picker in favor of its replacement; and `unavailable_fallback` routes a non-spawnable withdrawn model to a spawnable replacement at construction and overlay write time without retrying dynamic provider errors.

Regression assertions: [test contracts](../../packages/tests/lm/docs/contracts.meiva.okf.md#explicit-tuning).
