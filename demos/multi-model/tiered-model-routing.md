# Tiered Model Routing Demo

**What this shows**: `config_overlay` enables routing work to different models by phase — expensive models for planning and reflection, cheap models for execution and basic checks. This is cost-aware agent orchestration, not just side-by-side comparison.

## Prompt

```
You are an orchestrator. Your job is to complete a small coding project using tiered model routing: assign each phase of work to the right model based on the cost/quality tradeoff.

Pick a self-contained task — for example: "write a CLI tool that fetches a GitHub user's public repos and prints a summary table."

Phase 1 — Plan (expensive model):
Spawn a planner agent with a top-tier model. It must produce a concrete plan: file structure, key design decisions, a checklist of implementation steps.

```python
planner_id = ava.agents.spawn(
    prompt="Design a plan for: <your task>. Output: file tree, key decisions, implementation checklist.",
    config_overlay={"llm_model": "claude-opus-4-8"},
    label="planner"
)
```

Wait for the planner, then read its plan via `ava.agents.get_last_message(planner_id)`.

Phase 2 — Execute (cheap models, parallel):
Based on the plan, spawn 2-3 worker agents in parallel, each responsible for a different file or module. Use a cheap model — the work is straightforward implementation from a clear spec.

```python
worker_ids = []
for i, file_spec in enumerate(file_specs):
    wid = ava.agents.spawn(
        prompt=f"Implement this file per the plan:\n{plan}\n\nYour file: {file_spec}",
        config_overlay={"llm_model": "deepseek-v4-flash"},
        label=f"worker-{i}"
    )
    worker_ids.append(wid)
```

Collect all outputs.

Phase 3 — Check (mixed models):
- Spawn a **quality reviewer** with a strong model to review the combined output for correctness, edge cases, and code quality.
- Spawn a **functional checker** with a cheap model to verify the code runs and meets the spec.

```python
reviewer_id = ava.agents.spawn(
    prompt=f"Review this code for correctness, edge cases, and code quality:\n{combined_output}",
    config_overlay={"llm_model": "claude-sonnet-4-6"},
    label="reviewer"
)

checker_id = ava.agents.spawn(
    prompt=f"Verify this code meets the spec and would run correctly. Flag anything broken:\n{combined_output}",
    config_overlay={"llm_model": "deepseek-v4-flash"},
    label="checker"
)
```

Phase 4 — Reflect (expensive model):
Spawn a reflector with a top-tier model. Give it the full output — plan, implementation, review, check results — and ask: what architectural decisions held up? What would you change? What pattern should be reused next time?

```python
reflector_id = ava.agents.spawn(
    prompt=f"Reflect on this project: what worked, what didn't, what patterns to reuse.\n\nPlan:\n{plan}\n\nCode:\n{combined_output}\n\nReview:\n{review}\n\nCheck:\n{check}",
    config_overlay={"llm_model": "claude-opus-4-8"},
    label="reflector"
)
```

Final output:
Use `ava.ui.serve_markdown()` to present:
- The task and plan
- The code produced
- Review and check results
- Reflection
- A summary table of which model was used for each phase and why

Requirements:
- Each phase must use a different config_overlay with the appropriate llm_model
- Workers in Phase 2 must run in parallel
- The whole demo should complete autonomously — no human intervention between phases
```

## Expected flow

1. Orchestrator picks a small but realistic coding task
2. **Phase 1**: Spawns a claude-opus-4-8 planner → gets a concrete plan
3. **Phase 2**: Spawns 2-3 deepseek-v4-flash workers in parallel → each implements one file
4. **Phase 3**: Spawns claude-sonnet-4-6 reviewer + deepseek-v4-flash checker in parallel
5. **Phase 4**: Spawns claude-opus-4-8 reflector → architecture-level reflection
6. Renders the full pipeline output via `serve_markdown`

## Expected output

A Markdown report showing:

| Phase | Model | Cost tier | Why |
|-------|-------|-----------|-----|
| Plan | claude-opus-4-8 | $$$ | Strategic thinking needs strongest reasoning |
| Execute | deepseek-v4-flash | $ | Straightforward implementation from clear spec |
| Review | claude-sonnet-4-6 | $$ | Quality gate needs strong model, not the strongest |
| Check | deepseek-v4-flash | $ | Binary pass/fail verification |
| Reflect | claude-opus-4-8 | $$$ | Architectural insight needs deep reasoning |

Plus: the plan, the code, the review, and the reflection.

## Why this matters

This is the real use case for `config_overlay` model selection — not just "try three models and compare," but **cost-aware routing by cognitive demand**:

- **Spend on thinking, save on doing.** Planning and reflection are high-leverage; execution from a clear spec is low-leverage. Route accordingly.
- **One orchestrator, many models.** No global config changes. Each `spawn()` call carries its own model choice.
- **Parallelism + cost control.** Cheap workers run in parallel without multiplying costs at the expensive tier.
- **Portable pattern.** The same tiered routing works for research (plan=research design, execute=fact-gathering, reflect=synthesis), content creation, data analysis — any multi-phase intellectual task.
- **Agent chooses the model, not the user.** Most frameworks force the user to pick a model before starting — Claude Code only supports Anthropic models, and switching requires manual config changes. Ava can ship a "model router" skill: the agent loads it, inspects the task's cognitive demand and budget, and picks the right model autonomously. The user says *what* to do; the agent decides *which model* to use for each phase.
