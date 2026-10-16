---
type: doc
title: ava-immersive skill — live sessions with the human present
description: An opt-in, human-triggered live-session discipline — silence cap, delegation of slow work, a durable ledger, and explicit exit back to the default batch reporting.
tags: [extensions, agent-instruction]
---

# ava-immersive skill — live sessions with the human present

## What it is

`ava-immersive` keeps a live human conversation continuously fed while heavy
work runs in parallel. The human decides and steers; the agent carries the
context and the pacing, so the human can drop in and out without losing the
thread. It is entered only by the human ("immersive on"), announced on entry
(silence cap, inline/delegate split, exit), and scoped to the session; exit
reverts to the default reporting discipline.

## Relationship to the default reporting discipline

The default (`reduce-context-switch-for-human`) keeps the human unbothered
while they are away: results queue, updates batch, nothing depends on their
attention. Immersive mode is the other regime of the same attention surface,
used while the human is present. Both regimes keep heavy work parallel; they
differ only in when information reaches the human.

## Boundaries

- Entry is human-triggered; the agent may offer a live session once, but cannot
  place the human's attention there itself (2026-10-17 user ruling).
- Session-scoped: only the session's agents and tasks; exit is explicit.
- Open gap: platform routing has no per-session do-not-disturb yet; the
  foreground agent filters incoming fleet updates itself.
- v1 scope: the foreground agent reads, checks, and converses; code, builds,
  and research go to workers.

## Validation

Cases in `evals/evals.json`, measures in `evals/metrics.md`. The first
evidence run is a live session with the requester (2026-10-17); results are
appended before merge.

[[ava_builtins/skills/docs/skills.ava.okf.md|Skills index]]
