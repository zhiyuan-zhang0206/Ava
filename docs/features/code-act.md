# CodeAct

Ava agents act by **writing code**, not by picking from a menu of tools. One
`execute_code` tool plus the whole `ava.*` Python namespace is every capability:
files, network, search, memory, even spawning other agents.

## Why it matters

- **Maximum expressiveness** — loops, conditionals, composition; the agent
  orchestrates with real control flow, not a fixed menu of tool calls.
- **No per-capability schemas** — one wire shape for every action, nothing to
  escape and nothing to maintain per tool.
- **Code is the interface** — anything a Python program can do, an agent can do;
  a `for`-loop can spawn and coordinate an entire fleet.

## How it works

```
agent writes Python → execute_code(code) → runs in the agent process
    → ava.* namespace (shell, files, web, memory, agents, watcher, ...)
    → stdout/stderr + structured metadata come back as one tool result
```

<!-- TODO(image): single execute_code tool → ava.* namespace diagram -->

## Real usage

- [`demos/dynamic-workflow/landscape-research-case-study.md`](../../demos/dynamic-workflow/landscape-research-case-study.md) — a real production run:
  the agent decomposed a research goal into waves of workers, each writing a
  result file and terminating; gather watchers woke the orchestrator only when
  each wave settled.
- [`demos/goal-mode/goal-mode-code-review.md`](../../demos/goal-mode/goal-mode-code-review.md) — completion-judged goal mode.

## Design decisions

- [Single execute_code tool](../../decisions/2026-05-04-single-execute-code-tool.md)
