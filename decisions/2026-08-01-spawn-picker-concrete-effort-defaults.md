# Spawn picker shows concrete per-model effort defaults (no "Effort: default" option)

Date: 2026-08-01
Decision by: the maintainer, via task #568
Status: adopted

## The problem

The spawn dialog's reasoning-effort select always led with a synthetic
"Effort: default" (`""`) option meaning "the provider's own server-side
default". The user's words (2026-08-01, translated): *"after we pick a model, its default
effort should be selected directly. Why is there a separate 'effort:default' option at
all? That is simply wrong."* — selecting a model should bring up that model's default effort, not a
vague standalone option. And a second defect in the same dialog: picking a
preset (e.g. Ultra Speed Worker, whose config pins `llm_model`) left the model
picker on whatever was selected before — *"when a preset is picked, it should overwrite
the model's settings along with everything else"*.

The first half is the design question left open by #1009 (rejected) and #1046
(closed): `""` = "provider default" is a real state, but it has no concrete rung
to display. #1009's attempt to publish `effort_levels[0]` (the ladder FLOOR,
weakest-first) as the default was rejected by the owner because it silently
downgraded 18/20 models (claude → low, gpt/mimo → none = reasoning off,
gemini → minimal).

## Decision 1: every spawnable model pins a concrete default effort

The registry's per-model `ModelTuning.reasoning_effort` now carries a concrete
value for every spawnable model (validated at import: a spawnable model without
one fails fast), using the vendor-documented defaults already recorded in
`2026-07-25-per-model-tuning-values.md` Decision 4:

| family | default |
|---|---|
| deepseek-v4-pro / -flash | `max` (already pinned; Ava is not an auto-promoted harness) |
| claude-sonnet-5 / opus-5 / fable-5 | `high` (Anthropic's documented default; closest concrete rung to adaptive — NOT the floor) |
| claude-haiku-4-5-20251001 | `none` (extended thinking defaults OFF) |
| gemini-3.1-pro-preview | `high` (documented `thinking_level=high`, cannot drop to minimal) |
| gemini-3.6-flash / 3.5-flash | `medium` |
| gpt-5.6-sol / terra / luna | `medium` (OpenAI's default) |
| mimo-v2.5-pro / -ultraspeed | `high` (the provider default: thinking already on) |
| kimi-k3 | `max` |
| glm-5.2 | `max` |
| grok-4.5 | `high` |

`GET /api/models` publishes the per-model tuning layer as
`reasoning_effort_default` (resolved via the registry layering with the
explicit-env layer excluded — the picker shows the MODEL's default, while a
cluster-wide `AVA_REASONING_EFFORT` pin stays operator policy visible in the
config panel's per-model view).

Consequences, accepted:

- The "follow a vendor that changes its own default" property of leaving the
  field `None` (Decision 4, 2026-07-25) is traded away for displayability.
- Spawning claude adaptive models from the UI now sends an explicit
  `effort: "high"` instead of omitting the field (provider adaptive). `high` is
  the documented default, so this is a pin, not a downgrade. Operators who want
  adaptive back can pin `AVA_REASONING_EFFORT=""` cluster-wide, or edit the
  registry value.
- The `""` state remains expressible everywhere it matters: as an explicit
  `AVA_REASONING_EFFORT=""` config pin, and in the frontend's legacy fallback
  (a model with a ladder but no concrete default keeps the "Effort: default"
  option — no catalog model today).

## Decision 2: the picker always shows and sends a concrete level

The select renders the ladder only, with the effective value = stored explicit
selection if still on the model's ladder, else the model's published default.
A spawn carries that level (what you see is what is sent). The stored setting
is left untouched by re-derivation, so switching A → B → A restores the user's
explicit choice on A.

## Decision 3: selecting a preset overrides the model pickers

A preset whose `config` carries `llm_model` / `reasoning_effort` writes those
values into the stored `behavior.spawn_model` / `behavior.spawn_reasoning_effort`
settings on selection, so the pickers visibly follow the preset. The preset
remains the seed; the backend merge (`{**preset_config, **explicit_config}`)
keeps explicit later picks winning per key. To make that true even when the
explicit pick equals the cluster default, the picker now ALWAYS sends a stored
model selection (previously the default-model selection was omitted as
equivalent — with a preset pinning a different model, omitting let the preset's
model silently win over what the user visibly picked).
