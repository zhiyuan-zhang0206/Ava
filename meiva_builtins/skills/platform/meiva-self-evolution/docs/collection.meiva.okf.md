---
type: doc
title: Self-evolution collection — a run's task and the inbounds it reads
description: Task-prompt origin and inbound scoping for the self-evolution dataset; the skill page owns the loop, this page owns what counts as a run's task.
tags:
- extensions
- agent-instruction
---

# Self-evolution collection

`collect.py` reads each run's inbounds and `record.py` turns them into the
dataset record; both feed the loop described on the
[skill page](meiva-self-evolution.meiva.okf.md). A run's task text is the
instruction it was working from, not merely the message that happened to land
inside the collection window — most runs wake from a resurrect, a heartbeat,
or a long-lived brief written days before the window opened.

`collect.py --days N` reads a whole window; `collect_one` builds the same
record for one agent over the last 7 days, which is how a freshly spawned
evaluation agent is scored. Both share the scoping below.

Parent: [[meiva_builtins/skills/platform/meiva-self-evolution/docs/meiva-self-evolution.meiva.okf.md]].

Every record carries the instruction its run works from. A human source (`user`, plus the pre-2026-06-19 web console `ui:web`) outranks the agent's own spawner, which outranks a `schedule:` sender; the earliest message of the winning kind supplies the text verbatim, and `task_prompt_source` names the winner. Resurrect, heartbeat, restart and fork inbounds are empty by construction, so a run driven by one falls through to the transcript's first human-authored message that is not the engine's system preamble — the inherited task, marked `transcript`. A task taken from a human message consumes exactly that message: every later human message is a follow-up or a correction, never a second task. `task_prompt_of` applies the transcript fallback at read time, so records written before this rule still expose the task their conversation shows.

`_inbounds_by_agent` merges two row sets in `created_at` order: the earliest chat from each source that can carry a task — the agent's spawner from `agents_meta`, a human source, any `schedule:` sender — which may predate the window, as a long-lived agent's brief always does; and every chat inbound inside `[window_from, window_to)` from a human or `agent:` source. Both queries take the caller's window instead of `now()`: a backfilled tile must read its own dates, not the day the backfill ran. Reading only those few historical rows keeps `corrections` and `peer_feedback` window-scoped (the 2026-08-13 lifetime-history regression), and a broadcast instruction stays visible without counting as a per-agent correction. Runtime notices from `shell:`, `watcher:` and `system` are not task origins and are never read as one.
