---
type: doc
title: Effective Agent Model Configuration
description: Shared model and effort validation for birth, fork, launch and restart.
tags:
- base
- llm-inference
---

# Effective Agent Model Configuration

`base/agents/birth_config.py:resolve_default_model` resolves the DB-backed
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
