# Per-model config registry — everything per-model, shared fallback

**Decision (2026-07-25).** Every tunable that plausibly varies by model is
per-model by default, resolved through one registry
(`shared/lm/registry.py:MODELS: dict[id, ModelSpec]`) with a shared-default
floor. The layering, weakest first:

1. **Code shared default** — `DEFAULT_TUNING`, a fully-populated `ModelTuning`
   row carrying the former pydantic field defaults (0.8/0.6 compact fractions,
   retry 6, TTFT 30s, style `oriented`, every guidance section on, …).
2. **Per-model default** — `MODELS[id].tuning`, a code table; `None` = no
   per-model opinion.
3. **Explicit `.env` / env value** — the user's deliberate global choice; pins
   the whole cluster regardless of model.
4. **Per-agent overlay** — spawn/restart `config_overlay`, unchanged mechanics
   (`set_field` onto the settings singleton at boot).

Driver: models behave too differently for one global default — GPT-family
models put their heads down and work, Claude-family models narrate heavily,
weaker models need more guidance sections and longer prompts, and the compact
threshold in particular cannot be one global fraction ("just go with one overall fraction and be done with it" was explicitly rejected): a model that stays coherent deep into its window should compact at 0.9, one that degrades early at 0.6. Both factors of the
compact threshold are now per-model: fraction × window
(`context_budget.resolve_context_budget`).

## Why a code table, not a config dimension

A per-model override *store* (config rows keyed by model id) is exactly the
`model_overrides` shape migration 0047 retired. It re-creates the "which layer
holds the truth?" ambiguity, needs schema + UI + bootstrap distribution, and
its content would still be maintained by the same people who maintain the
registry. Per-model defaults are engineering judgments about model behavior —
code, reviewed in PRs, shipped with the version of the framework whose prompts
they tune. The "one value lives in exactly one place" invariant holds: shipped
defaults live in the registry, the user's global choice lives in `.env`, a
per-agent choice lives in the overlay.

## The explicitness sentinel: `None` field defaults, not `model_fields_set`

To let an explicit `.env` value beat a per-model default, the resolver must
know whether the user actually set the field. Two candidate mechanisms:

- **`model_fields_set` (rejected).** pydantic tracks which fields came from a
  source vs a default — attractive because nothing about the field types
  changes. But it is **topology-dependent**: a split agent-runner receives its
  config via `/api/bootstrap` injected into `os.environ`, and
  `bootstrap_config_values` serves *every* bootstrap field (unset fields as
  their stringified boot-time values). On a runner everything would look
  explicitly set and the per-model layer would silently never engage, while a
  single box resolved differently.
- **`None` sentinel on the field (chosen).** Per-model-defaultable settings
  fields become `T | None = None`; their former defaults move to
  `DEFAULT_TUNING`. Unset serializes as absent (`bootstrap_config_values`
  skips `None`), so "unset" survives every distribution path as data, and any
  real source — `.env`, exported env, bootstrap-forwarded value, per-agent
  overlay — writes a non-None value. An explicitly *empty*
  `AVA_REASONING_EFFORT=` still parses to `""` ≠ None, preserving "explicitly
  choose the provider default" as distinct from unset.

Cost of the sentinel: the config panel's `default_value` for these fields is
now `null` (the effective floor lives in the registry, noted in each field's
description), and `get_config_metadata` unwraps `T | None` so the panel keeps
rendering the typed editor (bool switch / float input / enum select).

## Registry convergence

`shared/lm` had ~12 parallel per-model-id tables (`SUPPORTED_MODELS`,
`MODEL_CONTEXT_WINDOW`, `MODEL_KNOWLEDGE_CUTOFF`, `_MODEL_DEFAULT_STREAMING`,
`_CLAUDE_MAX_TOKENS`, `_DEEPSEEK_MAX_TOKENS`, `_CLAUDE_EFFORT_LEVELS`,
`_CLAUDE_EXTENDED_THINKING_ONLY`, `_CLAUDE_EXTENDED_THINKING_EFFORT_LEVELS`,
`MODEL_PRICING`, plus the gateway's hand-kept effort-options dicts) whose
memberships had drifted (models present in pricing but not windows, spawnable
models missing cutoffs at various points). They are one `ModelSpec` entry per
model now; the legacy names survive as derived views so import sites and the
wire surface are unchanged, and import-time invariants (`_validate_registry`)
make the drift class impossible for spawnable models. Deliberately NOT folded
in: per-provider facts (prefix → API key map, the OpenAI-style providers' wire
effort vocabularies, vision prefixes) — those are endpoint contracts, not
per-model facts, and stay in `factory.py` / `_effort.py`.

## Rejected alternatives

- **Per-model env namespace** (`AVA_<MODEL>_AUTO_COMPACT_FRACTION`): unbounded
  env surface, un-typed, un-distributable, and still needs a shipped-defaults
  table underneath.
- **Keeping per-model knowledge only where it already existed** (windows,
  caps) and adding one-off tables per new field: that is how the 12-table
  drift happened.
- **Changing bootstrap distribution to serve only `.env`-set fields** (to make
  `model_fields_set` viable): changes the multi-machine config contract for
  every field to serve one feature; the sentinel is strictly local.

## Rollout

The mechanism landed with **every per-model tuning value = None** — zero
behavior drift (rendered system prompt verified byte-identical; derived views
verified equal to the former tables). The actual per-model default matrix
(which model families get which guidance sections / styles / fractions) is
owner-reviewed content, proposed separately in the PR body and filled in only
after review.

<!-- Extended (not overturned) by:
decisions/2026-07-31-per-agent-config-lifecycle.md — layer 4's per-agent
scope gained a lifecycle axis. A `frozen` field's layer-3 value is captured at
agent birth into `agents_meta.birth_config` and replayed for that agent's life,
so a later `.env` / cluster-default change reaches only agents born after it;
`live` fields resolve exactly as described here. -->
