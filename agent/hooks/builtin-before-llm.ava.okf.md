---
type: doc
title: Built-in before_llm Hooks
description: The three unconditionally registered built-in before_llm hooks — compact reminder (no model call), repair (dangling tool pairing), capability-index drift (newly installed skills note) — and their registration order.
tags:
- agent
- hooks
---

# Built-in before_llm Hooks

- Built-in compact reminder (`agent/hooks/compact.py:_CompactReminderHook`, registered via `register_compact_hooks()`) performs no model call. It injects the once-per-window wind-down reminder below the hard ceiling and defers above it. Automatic summarization runs inside the LLM node, using the same durable-interrupt race as ordinary generation. Interrupted summarization preserves history and the compact version; claim retains command ownership. A successful summary rebuilds context through `init_context`, then returns to claim before the next model request. Agent-authored `compact_summary` inbounds still apply at claim.
- Built-in repair (`agent/hooks/repair.py:_RepairDanglingToolPairingHook`, `register_repair_hooks()`) is also an unconditionally registered built-in before_llm hook, deliberately registered **before compact**—it reconstructs a dangling tool_use with an interrupted result or drops a tool_result whose carrying tool_use was lost, before the LLM node can submit either normal generation or compaction
- Built-in capability-index drift (`agent/hooks/capabilities.py:_NewlyInstalledSkillsHook`, `register_capabilities_hooks()`) is the third unconditional built-in before_llm hook, registered **last**—it appends a note to whatever history survives repair's guard. It names the skills installed since the `# Capabilities` index was rendered, which is a snapshot while the skill catalog is a live filesystem scan; drift against `state.capabilities.indexed` is the trigger, and the snapshot advances with the note so one install is named once. It **defers** (returns `None`, writing nothing) when `auto_compact_will_fire(state)`, the shared gate used by reminder plugins. The LLM node will summarize and rebuild the head, including the current capability index, so an extra drift note would be immediately discarded. Also suppressed in container/eval mode (`ops_pool is None`). See [[agent/graph/system-prompt.ava.okf.md]]

Parent: [[agent/hooks/hooks.ava.okf.md|Hooks]].
