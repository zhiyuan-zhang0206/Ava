---
type: doc
title: ava-dynamic-workflow skill — script orchestration
description: Executable peer dispatch, collection, verification, and reduction with proportionate setup and explicit recovery limits.
tags:
- extensions
- agent-instruction
---

# Dynamic Workflow

Dynamic Workflow is a script written by an ordinary persistent peer. Independent
work can benefit from concurrency without requiring a script or one peer per
item. The skill starts with one representative dispatch/delivery/verification
path before expanding. The current agent freely chooses execution, orchestration,
and evaluation roles; no Fleet labels or task records are prerequisites.

The entrypoint carries method selection; `references/execution.md` carries
dispatch, collection, verification, budget handling, and cleanup. Read it when
script orchestration is selected.

`references/minimal_dispatch.py` provides a single-writer starting example. It
stores intent and peer receipts, preserves validated task/input-matching results,
and stops on ambiguous dispatch instead of automatically repeating remote work.
Local file writes and remote spawn are not atomic: arbitrary-crash exactly-once
execution is not claimed. Known assignments require deliberate reconciliation or
retry, not a missing-file loop. A dispatch failure stops before waiting.

`references/gather_files.py` wakes at file-existence checkpoints. Readiness must
be followed by identity, version, schema, and domain-evidence validation. Results
are written atomically, completed artifacts retained, and watcher IDs recorded
for re-entry. Blockers, budget decisions, and handoffs still reach the responsible
peer directly. Idle and self-termination are optional lifecycle choices.

The one-shot orchestrator template and larger research/sweep demos remain
illustrations, not resumable defaults. Read references for the current gap.

USD accounting uses the existing long-running-agent usage script and explicit
IDs, birth lineage, and windows. Reminders ask for decisions; preserve handoffs
and pause state before more dispatch rather than hard-killing peers. Resume
conditions and scope survive late results and script re-entry.

## Owners

- [Skill](../SKILL.md) owns method selection and orchestration guidance.
- [[ava/agents/docs/agents.ava.okf.md|Agents]] owns peer lifecycle and communication.
- [[ava/docs/watcher.ava.okf.md|Watcher SDK]] owns checkpoint execution.
