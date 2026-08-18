# Ava Demos

Copy-paste prompts that showcase Ava's core orchestration capabilities.
Each demo lives in its own category folder under `demos/<category>/`.

## What's here

| Demo | Category | Checkpoint signal | Scale | What it shows |
|---|---|---|---|---|
| **Deep Research + Cross-Check** | (orchestrator script) | Result files | 40 agents, 7 waves | Multi-wave research with feedback loop |
| **Landscape Research (case study)** | `dynamic-workflow/` | Result files + watchers | 18 agents, 3 waves | A real production run: 6 orchestrator wake-ups, live timeout recovery |
| **Codebase Sweep** | `dynamic-workflow/` | Agent lifecycle | 28 agents, 7 waves | Real code scanning with adversarial review |

| **Goal Mode — Code Review** | `goal-mode/` | Goal supervision | 2 agents, multi-turn | Evaluator-optimizer pattern (spec + three recorded real runs) |
| **Long-Running — Health Monitor** | `long-running/` | Cron + state | 1 agent, perpetual | Agent as background service |
| **Multi-Model Switching** | `multi-model/` | Config overlay | 3 agents | Side-by-side model comparison |
| **Tiered Model Routing** | `multi-model/` | Config overlay | Multi-agent | Cost-aware model routing by phase |
| **Chrome MCP** | `chrome-mcp/` | Browser | 1 agent | Shared browser context between user and agents |
| **Permission Hooks** | `permission-hooks/` | Hooks | 1 agent | Sensitive-operation gate examples |

> Orchestrator reference scripts live at `ava_builtins/skills/ava-dynamic-workflow/reference/`.
>
> **Note (2026-08-08)**: `dynamic-workflow-travel.md` is a stale duplicate of the
> Deep Research prompt (the described Travel Booking demo was never written);
> the checkpoint for Codebase Sweep is the same `gather_files` file-polling
> watcher as every other dynamic workflow — "agent lifecycle" below is a
> description of what the polled file tracks, not a separate watcher type.

## Folder layout

```
demos/
├── README.md
├── chrome-mcp/
│   └── chrome-mcp-overview.md
├── dynamic-workflow/
│   ├── dynamic-workflow-travel.md
│   ├── codebase-sweep.md
│   ├── landscape-research-case-study.md
│   └── landscape-run-output.png
├── multi-model/
│   ├── multi-model-config-overlay.md
│   └── tiered-model-routing.md
├── goal-mode/
│   ├── goal-mode-code-review.md
│   ├── 2048/
│   │   ├── index.html
│   │   ├── game.js
│   │   ├── style.css
│   │   └── test.js
│   ├── snake/
│   │   ├── index.html
│   │   ├── game.js
│   │   ├── style.css
│   │   └── test.js
│   ├── weekly-planner/
│   │   ├── index.html
│   │   ├── app.js
│   │   ├── store.js
│   │   ├── style.css
│   │   └── test.js
│   └── ledger/
│       ├── index.html
│       ├── app.js
│       ├── store.js
│       ├── style.css
│       └── test.js
├── long-running/
│   └── long-running-health-monitor.md
└── permission-hooks/
    ├── README.md
    └── sensitive_op_gate.py
```

## One completion protocol, two checkpoint signals

Every worker in every dynamic workflow finishes the same way: **write the result
file, then `ava.self.terminate()` — silently**. No worker messages the orchestrator;
N workers reporting individually would wake the orchestrator N times for N LLM turns.
The orchestrator instead arms **checkpoints** — as few as one, at the places it
actually wants to wake up — and each checkpoint reports a whole batch in one message.

What a checkpoint watches is the choice left:

| Checkpoint signal | Fires when | Pros | Cons |
|---|---|---|---|
| **Result files** (Deep Research) | The files it names exist — all of them, or K of N | Ground truth, survives agent crash | Polling overhead, stale files need cleanup |
| **Status files** (Codebase Sweep) | The wave's status file reports all agents terminated | Clean lifecycle, natural fit | Ambiguous (crashed vs done), needs the result file as backup |

## How to use

1. Open the demo folder
2. Copy the prompt from the markdown file
3. Send it to an Ava agent (or spawn a new one with `ava.agents.spawn(prompt=...)`)
4. Watch the agent orchestrate its fleet

## For contributors

Each demo lives in its own folder under `demos/<category>/`. Add new demos as
`demos/<category>/<name>.md` files with this structure:

```markdown
# <Title>

**What this shows**: <one-line summary of the Ava capability>

## Prompt

<the exact prompt to give to an Ava agent>

## Expected flow

<what the agent will do, step by step>

## Expected output

<what the user sees at the end>

## Why this matters

<why this capability is uniquely strong in Ava vs alternatives>
```
