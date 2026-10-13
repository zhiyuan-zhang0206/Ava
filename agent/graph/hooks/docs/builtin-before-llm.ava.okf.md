---
type: doc
title: Built-in before_llm Hooks
description: The unconditionally registered built-in before_llm hooks — repair (dangling tool pairing), compact reminder (no model call), skill recall (one decision-model request), capability-index drift (newly installed skills note) — and their registration order.
tags:
- agent
- hooks
---

# Built-in before_llm Hooks

- Built-in compact reminder (`agent/graph/hooks/compact.py:_CompactReminderHook`, one of `framework_hooks()`) performs no model call. It injects the once-per-window wind-down reminder below the hard ceiling and defers above it. Automatic summarization runs inside the LLM node, using the same durable-interrupt race as ordinary generation. Interrupted summarization preserves history and the compact version; claim retains command ownership. A successful summary rebuilds context through `init_context`, then returns to claim before the next model request. Agent-authored `compact_summary` inbounds still apply at claim.
- Built-in repair (`agent/hooks/repair.py:_RepairDanglingToolPairingHook`, ) is also an unconditional built-in before_llm hook, deliberately ordered **before compact**—it reconstructs a dangling tool_use with an interrupted result or drops a tool_result whose carrying tool_use was lost, before the LLM node can submit either normal generation or compaction
- Built-in skill recall (`agent/graph/hooks/skill_recall.py:SkillRecallHook`) is ordered **before** the drift check. On a turn woken by a person or a schedule it scores the skill index against the recent conversation with one decision-model request and names the best matches in one `skill_recall` note. It keeps its own per-window record (`skill_reads`, `skill_recall`) and clears it after a compaction, defers on `auto_compact_will_fire(state)`, and degrades every failure or timeout (`skill_recall_deadline_seconds`) to no note. See [[agent/graph/docs/skill-recall.ava.okf.md]]
- Built-in capability-index drift (`agent/graph/hooks/capabilities.py:_NewlyInstalledSkillsHook`, ) is the last unconditional built-in before_llm hook, ordered **last**—it appends a note to whatever history survives repair's guard. It names the skills installed since the `# Capabilities` index was rendered, which is a snapshot while the skill catalog is a live filesystem scan; drift against `state.capabilities.indexed` is the trigger, and the snapshot advances with the note so one install is named once. It **defers** (returns `None`, writing nothing) when `auto_compact_will_fire(state)`, the shared gate used by reminder plugins. The LLM node will summarize and rebuild the head, including the current capability index, so an extra drift note would be immediately discarded. Also suppressed in container/eval mode (`ops_pool is None`). See [[agent/graph/docs/system-prompt.ava.okf.md]]

Parent: [[agent/hooks/docs/hooks.ava.okf.md|Hooks]].
