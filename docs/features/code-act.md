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

## Measured evidence (2026-08-27, fleet-wide)

A controlled two-arm benchmark (deepseek-v4-flash, N=2 per task class) plus a
200-worker production counterfactual established the headline economics of
writing code vs. calling tools:

- **Fewer calls, fewer tokens.** Packing multi-step work into one `execute_code`
  call cuts tool calls to 0.63–0.76× and input tokens to ~0.65–0.68× across task
  regimes, with task correctness unchanged (12/12 mid, 4/4 long tasks).
- **Token savings on real traffic.** Applied to the 200 most recently active
  workers (17h, 1.26B input tokens), CodeAct saves **388M input tokens (30.8%)**;
  the long-regime workers (96 turns / 8.15M tokens median, 99.1% cache ratio)
  carry 82% of the cost and nearly all of the savings.
- **Cost savings.** Counterfactual total cost drops **-12.9% (neutral) to
  -17.7% (model-based)**; 143 of 199 workers get cheaper, and the 56 that get
  slightly pricier are all short tasks (≤0.5M tokens, 1.4% of total cost). The
  savings come from fewer cached-prefix re-reads: every avoided turn skips one
  re-send of the growing context at the provider's cache-read price.
- **Compaction does not erode the savings.** Measured around real compactions:
  cache ratio 99.9% → 92.7% on the first post-compact call, recovered by the
  third; the cold pass costs ~$0.001 (≈0.1% of a long run) — the savings are on
  the warm turns that dominate the bill.
- **Vendor transfer.** On Anthropic pricing (cache read 10× discount vs
  DeepSeek's 30×), the same CodeAct deltas save **-23.0%** of a 12.4× (Sonnet-5)
  or 31× (Opus-5) larger bill — fewer turns is worth more wherever re-reads are
  more expensive.

Data and scripts: `~/.ava/workspaces/1289/codeact-bench/` (q3/q5/q6 scripts,
llm_usage event dumps); user-facing report at the cluster's page server.

## Design decisions

- [Single execute_code tool](../../decisions/2026-05-04-single-execute-code-tool.md)
