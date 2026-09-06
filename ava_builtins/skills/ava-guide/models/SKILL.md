---
name: models
description: Chooses the LLM tier and config overlay for spawned Ava workers under the current cost policy, which only picks models on the intelligence Pareto frontier. Use before every worker spawn or dynamic workflow, even when the model choice seems obvious.
---

# Model Selection — the Pareto Frontier and Tier Policy

You pick a worker's model at spawn time:

```python
ava.agents.spawn(prompt="...", config_overlay={"llm_model": "gemini-3.7-flash"})
```

Omitting the overlay is a valid choice — the child inherits the cluster default.
The registry (`shared/lm/registry.py`) is the authoritative list of available
models (`spawnable=True`) and their prices; this skill carries the judgment the
registry cannot: **which tier a given sub-task deserves, and which registered
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
  selectable iff `spawnable=True`; a current price means a matching
  `pricing_catalog.json` entry. The frontend picker and `/api/models` both
  derive from this registry, so the registry is the only place a model is born.
- **Presets are not models.** `ava presets ls` / `ava.agents.presets.list()`
  list named config templates (which may carry an `llm_model`), not the model
  roster. There is no dedicated `ava models` CLI yet — the API above or the
  registry is how an agent lists models.

The table below names concrete ids as **policy** — the standing tier choices,
not the full roster. Before spawning, confirm the id exists in the registry;
if this doc names a model the roster no longer has, update the doc — the
registry is maintained as it ships, this skill is maintained as policy.

## Pareto frontier principle

Only models on the intelligence Pareto frontier are eligible: a model is out
once another registered model dominates it — strictly smarter at the same or
lower cost, or equally smart at lower cost. Picking a dominated model is a
policy bug, not a preference, so the frontier is actively maintained:

- When a new model lands, place it on the frontier **only if** it is not itself
  dominated; register it, then update this skill's table.
- When a new model dominates a current one, replace it in this skill's table and
  note the dominance; the old model stays registered (older configs keep
  working, and `superseded_by` hides it from the picker) but is no longer a
  choice this policy names.
- A dominance pair is worth stating explicitly, so a later reader does not
  resurrect the dominated name out of habit.

Standing dominance (user ruling 2026-09-03):

- `deepseek-v4-flash-vision-exp` **dominates** `deepseek-v4-flash` — identical
  price (the catalog carries the same rates), same 1M context, plus vision and
  strictly better intelligence. There is no reason to select
  `deepseek-v4-flash` again: wherever a doc, script or spawn choice names it,
  use the vision-exp id.

## Current cost policy

The standing pairing is (subject to change — check with the user before
reaching for anything more expensive):

| Tier | Model | Use for |
|---|---|---|
| **Judgment (main)** | `gemini-3.7-flash` | orchestration, planning, synthesis, reviewing/judging other agents' output, writing for humans |
| **Mechanical** | `deepseek-v4-flash-vision-exp` | high-volume parallel workers, extraction, format transforms, checklist verification, scanning/sweeping |

`gemini-3.8-flash` is spawnable on the production picker since 2026-09-06
(user order; fresh-spawn verified clean by agent #5834). Caveat: restarting an
EXISTING agent onto 3.8 (history written by another model) still 400s with
"Corrupted thought signature" — the cross-model message-projection protocol is
not landed yet, so switch only fresh agents to 3.8. The mechanical tier replaced
`deepseek-v4-flash` with its vision-exp sibling (same price, strictly smarter —
see the dominance pair above). Claude and other models stay registered and
spawnable, but they sit outside the default policy — use them only when the
user explicitly asks for them.

## How to decide the tier

Three questions about the sub-task:

1. **Open-ended judgment, or bounded procedure?** Decomposing a problem,
   weighing trade-offs, synthesizing prose → judgment tier. Clear inputs, a
   mechanical procedure, and a checkable done-condition → mechanical tier.
2. **Blast radius of a wrong answer?** A wrong orchestrator decision poisons
   every downstream worker → judgment tier at the top of the tree. One bad
   worker among ten parallel ones gets caught by a verification wave →
   mechanical tier at the leaves.
3. **Volume?** N parallel workers multiply cost by N — that is exactly where
   the cheap tier pays. One-off calls barely matter; fleets do.

The typical dynamic-workflow shape that falls out:

```
gemini-3.7-flash orchestrator
  → deepseek-v4-flash-vision-exp worker fleet
  → deepseek-v4-flash-vision-exp cross-checkers
  → gemini-3.7-flash synthesizer
```

## Don't

- Don't run a whole worker fleet on the judgment tier — a flash fleet plus a
  verification wave is cheaper and usually as accurate.
- Don't hand flash an open-ended judgment task and trust the output
  unverified — pair flash breadth with judgment-tier (or cross-flash) checking.
- Don't scatter hardcoded model names where the cluster default would do —
  an explicit overlay should mean a deliberate tier choice.
- Don't pick `deepseek-v4-flash` while `deepseek-v4-flash-vision-exp` is
  registered — same price, strictly smarter.
- Don't trust a model name from an old spawn, an old chat, or a stale doc —
  enumerate first (`GET /api/models` or the registry).
