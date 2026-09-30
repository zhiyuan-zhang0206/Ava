---
type: doc
title: Creation Availability
description: Spawn target gating, agent availability labels, and creation receipt wording in the frontend.
tags:
- frontend
---

# Creation Availability

`SpawnButton` reads `/api/status` and places agents only on online,
non-paused agent-runner machines with `agent_host_online=true`. The host
verdict keeps its existing pidfile meaning. A false or absent verdict leaves
the runner disabled with an explicit reason; when another runner is ready, the
blocked runner remains a disabled picker entry. With one eligible ready
runner, the button sends directly; with multiple eligible runners it opens a
popover. The disabled trigger's tooltip sits on a wrapper so it still appears
when the button itself cannot receive pointer events.

Spawn model, preset, and reasoning-effort selections are DB-backed
`behavior.spawn_*` settings. The effort select appears only when a resolved
model's `/api/models` entry publishes an `effort_levels` ladder. Every
spawnable catalog model publishes a concrete `reasoning_effort_default`; the
control shows the ladder's concrete levels with that default selected, without
a synthetic default option. Stored effort is re-derived against the current
model's ladder, so an unavailable level is never sent. A preset's
`llm_model` and `reasoning_effort` override the pickers when selected; a later
explicit pick still wins per key in the backend merge.

The selected conversation shows `AgentAvailability` above the pending strip
for fresh admission observations and host or admission anomalies, or for a
launch failure. It reads the selected agent's detail every 15 seconds because
host-probe changes need not emit agent lifecycle events. For non-launch reasons,
an observation older than two minutes hides the strip if detail refresh stops
succeeding. The freshness comparison tolerates a small future skew: a
just-fetched observation (stamped a beat after the client clock snapshot) shows
immediately instead of waiting for the next tick. Reason `unknown`, whether
fresh or missing evidence, also hides it.
Fresh host-down and refused labels link to Machine diagnostics. The labels
distinguish host admission from first-turn completion. Guide, preset,
schedule, and package-draft creation toasts say "created" and point to the
conversation for progress; they do not claim that a turn started.

A launch-failure reason stays visible regardless of probe age. The roster row
shows a launch-failed badge; the selected view shows the reason, failure time,
target machine, and Retry launch action. A structured create 502 selects its
committed `agent_id` and refreshes roster/detail instead of inviting another
create. Retry posts to the existing ID and reconciles the authoritative read.
