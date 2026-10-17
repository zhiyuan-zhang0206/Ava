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

```meiva-contract
id = "task-prompt-origin"
kind = "invariant"
code = [
  "meiva_builtins/skills/platform/meiva-self-evolution/scripts/record.py:_task_source",
  "meiva_builtins/skills/platform/meiva-self-evolution/scripts/record.py:_first_human_message",
  "meiva_builtins/skills/platform/meiva-self-evolution/scripts/record.py:_task_prompt",
  "meiva_builtins/skills/platform/meiva-self-evolution/scripts/record.py:_prompt_fields",
  "meiva_builtins/skills/platform/meiva-self-evolution/scripts/record.py:task_prompt_of",
]
tests = [
  "meiva_builtins/skills/platform/meiva-self-evolution/tests/test_self_evolution_record.py::test_task_prompt_origin",
  "meiva_builtins/skills/platform/meiva-self-evolution/tests/test_self_evolution_record.py::test_the_human_message_that_carried_the_task_is_not_a_followup",
  "meiva_builtins/skills/platform/meiva-self-evolution/tests/test_self_evolution_record.py::test_task_prompt_of_falls_back_to_the_transcript",
]
statement = """
Every record carries the instruction its run works from. A human source (`user`, plus the pre-2026-06-19 web console `ui:web`) outranks the agent's own spawner, which outranks a `schedule:` sender; the earliest message of the winning kind supplies the text verbatim, and `task_prompt_source` names the winner. Resurrect, heartbeat, restart and fork inbounds are empty by construction, so a run driven by one falls through to the transcript's first human-authored message that is not the engine's system preamble — the inherited task, marked `transcript`. A task taken from a human message consumes exactly that message: every later human message is a follow-up or a correction, never a second task. `task_prompt_of` applies the transcript fallback at read time, so records written before this rule still expose the task their conversation shows.
"""
```

```meiva-contract
id = "inbound-scoping"
kind = "invariant"
code = [
  "meiva_builtins/skills/platform/meiva-self-evolution/scripts/collect.py:_inbounds_by_agent",
  "meiva_builtins/skills/platform/meiva-self-evolution/scripts/collect.py:collect_one",
]
tests = [
  "meiva_builtins/skills/platform/meiva-self-evolution/tests/self_evolution_collect/test_task_prompt_origin.py::test_spawn_brief_older_than_the_window_is_the_task_prompt",
  "meiva_builtins/skills/platform/meiva-self-evolution/tests/self_evolution_collect/test_task_prompt_origin.py::test_shell_notice_does_not_shadow_the_spawn_brief",
  "meiva_builtins/skills/platform/meiva-self-evolution/tests/self_evolution_collect/test_task_prompt_origin.py::test_shell_session_notice_is_never_the_task_prompt",
  "meiva_builtins/skills/platform/meiva-self-evolution/tests/self_evolution_collect/test_task_prompt_origin.py::test_resurrect_wake_contributes_no_task_inbound",
  "meiva_builtins/skills/platform/meiva-self-evolution/tests/self_evolution_collect/test_task_prompt_origin.py::test_window_bounds_come_from_the_collection_window",
]
statement = """
`_inbounds_by_agent` merges two row sets in `created_at` order: the earliest chat from each source that can carry a task — the agent's spawner from `agents_meta`, a human source, any `schedule:` sender — which may predate the window, as a long-lived agent's brief always does; and every chat inbound inside `[window_from, window_to)` from a human or `agent:` source. Both queries take the caller's window instead of `now()`: a backfilled tile must read its own dates, not the day the backfill ran. Reading only those few historical rows keeps `corrections` and `peer_feedback` window-scoped (the 2026-08-13 lifetime-history regression), and a broadcast instruction stays visible without counting as a per-agent correction. Runtime notices from `shell:`, `watcher:` and `system` are not task origins and are never read as one.
"""
```
