---
name: models
description: Chooses the LLM model and config overlay for spawned Ava workers under the current cost policy, which only picks models on the intelligence Pareto frontier. Use before every worker spawn or dynamic workflow, even when the model choice seems obvious.
---

# Model Selection — the Pareto Frontier and Cost Policy

You pick a worker's model at spawn time:

```python
ava.agents.spawn(prompt="...", config_overlay={"llm_model": "deepseek-v4-flash"})
```

Omitting the overlay is a valid choice — the child inherits the cluster default.
The registry (`shared/lm/registry.py`) is the authoritative list of available
models (`spawnable=True`) and their prices; this skill carries the judgment the
registry cannot: **which model a given sub-task deserves, and which registered
models sit on the Pareto frontier.**

## Where model names come from — enumerate, never hardcode

The roster moves. Never grep the source for a model id and never trust a name
copied from an old doc or an old spawn; list the current roster first:

- **Live list (agents, scripts)** — `GET /api/models` returns the spawnable
  roster grouped by provider, with current pricing, context window, effort
  options and `superseded_by` — the same data the spawn picker renders:

  ```python
  import httpx, os
  r = httpx.get(
      f"{os.environ['AVA_GATEWAY_URL']}/api/models",
      headers={"Authorization": f"Bearer {os.environ['AVA_CLUSTER_SECRET']}"},
      timeout=10,
  )
  models = r.json()["models"]  # id -> {provider, context_window, pricing,
                               #         reasoning_effort_options, ...}
  ```

- **Source of truth (repo-side)** — `shared/lm/registry.py`'s `MODELS` dict and
  the derived `SUPPORTED_MODELS` (provider → spawnable ids). A model is
  selectable iff `spawnable=True`. Chat prices live in the provider plugin's
  `PriceRates` and are mirrored in `shared/lm/pricing_catalog_archive.json` —
  the reviewed ledger the runtime reads (catalog-only services such as
  embeddings price from the archive alone). The frontend picker and
  `/api/models` both derive from this registry, so the registry is the only
  place a model is born.
- **Presets are not models.** `ava presets ls` / `ava.agents.presets.list()`
  list named config templates (which may carry an `llm_model`), not the model
  roster. There is no dedicated `ava models` CLI yet — the API above or the
  registry is how an agent lists models.

This skill names concrete ids as **policy** — the standing choices, not the
full roster. Before spawning, confirm the id exists in the registry; if this
doc names a model the roster no longer has, update the doc — the registry is
maintained as it ships, this skill is maintained as policy.

## Pareto frontier principle

Only models on the intelligence Pareto frontier are eligible: a model is out
once another registered model dominates it — strictly smarter at the same or
lower cost, or equally smart at lower cost. Picking a dominated model is a
policy bug, not a preference, so the frontier is actively maintained:

- When a new model lands, place it on the frontier **only if** it is not itself
  dominated; register it, then update this skill's policy.
- When a new model dominates a current one, replace it in this skill's policy and
  note the dominance; the old model stays registered (older configs keep
  working, and `superseded_by` hides it from the picker) but is no longer a
  choice this policy names.
- A dominance pair is worth stating explicitly, so a later reader does not
  resurrect the dominated name out of habit.

Current frontier state (user ruling 2026-09-10):

- `deepseek-v4-flash` **dominates** `deepseek-v4-flash-vision-exp` — same
  price (the catalog carries the same rates), same 1M context, and its
  backend is DeepSeek V4.1 Flash. Wherever a doc, script or spawn choice
  named the vision experimental sibling, use the plain `deepseek-v4-flash`
  id: the policy is flash everywhere, vision work included.
- `deepseek-v4-pro` is **withdrawn from selection** (same ruling). It stays
  registered with `spawnable=False` so old configs keep working — it resolves
  to `deepseek-v4-flash` — but no new choice may name it.

## Current cost policy

**One model: `deepseek-v4-flash`** (user ruling 2026-09-10, superseding the
2026-09-03 two-tier table). Main agent and workers alike — orchestration,
planning, synthesis, review, extraction, format transforms, scanning — run the
same id on its V4.1 Flash backend.

- Complexity is absorbed by **decomposition and verification waves**, not by
  upgrading the model (next section).
- `deepseek-v4-pro` and `deepseek-v4-flash-vision-exp` are withdrawn by
  policy; vision work uses plain `deepseek-v4-flash`.
- Other registered models (`gemini-*`, Claude, GLM, Qwen, …) sit outside the
  default policy: select one only when the user explicitly asks for that
  model, and confirm it is on the roster first (section above).
- `gemini-3.8-flash` (trialed 2026-09-06, user order) is **stopped** under the
  still-valid stop ruling (pool: `model-policy-v2.3-gemini-flash-stop-20260903`)
  — do not select it. Historical caveat: switching an existing agent across
  models still 400s with "Corrupted thought signature"; switch only fresh
  agents — relevant only if an explicitly ordered exception ever switches
  models.
- An already-running agent still on a non-flash model is moved with
  `ava.self.restart(config_overlay={"llm_model": "deepseek-v4-flash"})`.
- Ruling record: shared-pool note `dev/ava-agent-model-flash-ruling-20260910.md`.

## How to run sub-tasks (flash-only)

There is no tier to pick; the same three questions now decide how much
structure a sub-task gets:

1. **Open-ended judgment, or bounded procedure?** Both run flash; open-ended
   work is split into smaller, checkable steps first.
2. **Blast radius of a wrong answer?** Large → add a verification wave (a
   flash cross-check, or peer review between agents) instead of a more
   expensive model.
3. **Volume?** N parallel workers multiply cost by N — batch the work and
   converge; that is where a flat policy pays. One-off calls barely matter.

The typical dynamic-workflow shape that falls out:

```
deepseek-v4-flash orchestrator
  → deepseek-v4-flash worker fleet
  → deepseek-v4-flash cross-checkers
```

## Don't

- Don't select anything outside `deepseek-v4-flash` for Ava-line agents
  (workers, orchestrators, reviewers, synthesizers alike) — `deepseek-v4-pro`,
  `deepseek-v4-flash-vision-exp` and `gemini-*` are all withdrawn from the
  policy (user ruling 2026-09-10 14:16), vision work included. An exception
  happens only when the user explicitly asks for a specific model (and it
  must be on the roster).
- Don't hand flash an open-ended judgment task and trust the output
  unverified — pair flash breadth with a flash cross-checking wave.
- Don't scatter hardcoded model names where the cluster default would do —
  an explicit overlay should mean a deliberate choice.
- Don't trust a model name from an old spawn, an old chat, or a stale doc —
  enumerate first (`GET /api/models` or the registry).
