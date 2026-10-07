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

Guide, preset, schedule, and package-draft creation toasts say "created" and
point to the conversation for progress; they do not claim that a turn started.
The conversation does not render a host-admission or launch-availability strip.

A launch-failure reason is still represented by the roster's launch-failed badge.
A structured create 502 selects its committed `agent_id` and refreshes
roster/detail instead of inviting another create.
