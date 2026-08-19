---
type: doc
title: Built-in before_llm Hooks
description: The three unconditionally registered built-in before_llm hooks — compact (force-compact near the context limit), repair (dangling tool_use history), capability-index drift (newly installed skills note) — and their registration order.
tags:
- agent
- hooks
---

# Built-in before_llm Hooks

- Built-in compact (`agent/hooks/compact.py:_AutoCompactHook`, registered via `register_compact_hooks()` from `build_graph()`) is a built-in before_llm hook: when approaching the context limit, it force-compacts via automatic summarization (if the agent actively calls `ava.self.compact()`, it writes a compact_summary inbound which claim directly replaces messages)
- Built-in repair (`agent/hooks/repair.py:_RepairDanglingToolUseHook`, `register_repair_hooks()`) is also an unconditionally registered built-in before_llm hook, deliberately registered **before compact**—the dangling tool_use message history it repairs is exactly the input that later hooks (e.g., force-compact's summarization call) might feed into the LLM
- Built-in capability-index drift (`agent/hooks/capabilities.py:_NewlyInstalledSkillsHook`, `register_capabilities_hooks()`) is the third unconditional built-in before_llm hook, registered **last**—it appends a note to whatever history survives repair's guard and compact's possible full replacement. It names the skills installed since the `# Capabilities` index was rendered, which is a snapshot while the skill catalog is a live filesystem scan; drift against `state.capabilities.indexed` is the trigger, and the snapshot advances with the note so one install is named once. It **defers** (returns `None`, writing nothing) on a pass where `auto_compact_will_fire(state)` — the shared gate the reminder plugins also call: `add_messages` applies compaction's `REMOVE_ALL` and then the append, so a note written alongside it would survive as the window's only message and make `init_context` mistake it for an intact history, dropping the parked summary and the SystemMessage with it. Deferring loses nothing—the compaction routes through `init_context`, which rebuilds the index from the catalog that now contains those skills. Also suppressed in container/eval mode (`ops_pool is None`). See [[agent/graph/system-prompt.ava.okf.md]]

Parent: [[agent/hooks/hooks.ava.okf.md|Hooks]].
