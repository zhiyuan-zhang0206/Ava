---
name: models
description: Choose which LLM a spawned agent runs on — tier judgment, the current cost policy (DeepSeek V4 pro/flash), and `config_overlay` spawn examples. Read before spawning workers in a dynamic workflow.
---

# Model Selection — the Right Model for Each Sub-task

You pick a worker's model at spawn time:

```python
ava.agents.spawn(prompt="...", config_overlay={"llm_model": "deepseek-v4-flash"})
```

Omitting the overlay is a valid choice — the child inherits the cluster default.
The registry (`shared/lm/registry.py`) is the authoritative list of available
models (`spawnable=True`) and their prices; this skill carries the judgment the
registry cannot: **which tier a given sub-task deserves.**

## Current cost policy

For price reasons the standing pairing is (subject to change — check with the
user before reaching for anything more expensive):

| Tier | Model | Use for |
|---|---|---|
| **Judgment** | `deepseek-v4-pro` | orchestration, planning, synthesis, reviewing/judging other agents' output, writing for humans |
| **Mechanical** | `deepseek-v4-flash` | high-volume parallel workers, extraction, format transforms, checklist verification, scanning/sweeping |

Claude and other models stay registered and spawnable, but they sit outside the
default policy — use them only when the user explicitly asks for them.

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
pro orchestrator → flash worker fleet → flash cross-checkers → pro synthesizer
```

## Don't

- Don't run a whole worker fleet on the judgment tier — a flash fleet plus a
  verification wave is cheaper and usually as accurate.
- Don't hand flash an open-ended judgment task and trust the output
  unverified — pair flash breadth with pro (or cross-flash) checking.
- Don't scatter hardcoded model names where the cluster default would do —
  an explicit overlay should mean a deliberate tier choice.
