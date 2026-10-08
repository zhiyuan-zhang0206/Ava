---
name: capability-timescale
description: "Calibrates AI-dependent feasibility and estimates against current evidence. Use when planning depends on an uncertain agent capability."
---

# Capability and timescale calibration

Before scheduling, estimating, or judging work whose pace depends on AI
capability, verify the assumptions that matter to the task. The system prompt's
Temporal awareness section routes here; Align and Plan use the same calibration.

## Gather evidence

1. Search available shared memory for recent capability findings, model/tool
   updates, and relevant verified runs. Check their dates and execution context.
2. Read current primary sources for the relevant model or tool. Search the web
   when release state or capability may have changed. Record what the evidence
   measures and whether it matches this task.
3. When practical, run a small representative probe using the actual tool and
   environment. Distinguish benchmark results from demonstrated task performance.
4. State what was verified, what remains an assumption, and how uncertainty
   affects feasibility or the plan. If current sources are unavailable, state
   that limitation instead of inventing a capability gain.

## Use the result

Carry the evidence into Align's scope and success criteria and Plan's task
choices and checkpoints. Recalibrate when execution contradicts an assumption.
Report progress and observed results without promising durations. Give a time
estimate when the user asks, with its basis and uncertainty range.

Avoid judging feasibility from training memory alone, treating an old benchmark
as current, or assuming a fixed capability ceiling or improvement multiplier.
