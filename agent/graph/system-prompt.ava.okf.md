---
type: doc
title: System Prompt — System Prompt
description: "The system prompt built once per context window. Constructed by `build_system_prompt()` in registration order: base guidance → SDK overview → behavior conventions → capability index."
tags: []
---

# System Prompt — System Prompt

## What it is

The system prompt carried in every LLM call, built **once per context window** — `init_context` renders it when it lays the standing head down (an agent's first wake, and the turn after each compaction), never per turn. Constructed by `build_system_prompt()` in registration order: base guidance → SDK overview → behavior conventions → capability index.

## Core Mechanism

### build_system_prompt (`_system_prompt.py:build_system_prompt`)
- The base is the `_BASE_SYSTEM_PROMPT` constant in `agent/graph/_llm.py` (the `{_AVA_OVERVIEW}` placeholder injects the SDK overview)—**it never reads `AGENTS.md` at runtime** (that file is for coding agents)
- Appends SDK documentation (the output of `ava.help(ava)`)
- Runs all registered section functions in fixed order
- Injects the skill and MCP server index — **once**. `# Capabilities` is the sole index; the expanded SDK reference's `*` skips `ava.skills` / `ava.mcps` (`_CAPABILITY_SURFACES`) so it renders call contracts only, never a second capability listing

### Plugin Registration (`register_system_prompt_section`)
- Signature `() -> str`, returning `""` means no contribution
- Runs in registration order—order is priority
- Framework's built-in sections are grouped: SDK detail → Conversation → Conduct → Capabilities

### Framework Built-in Sections

**Conduct group**:
- `_prefer_sdk_section` — "Prefer SDK"
- `_communication_style_section` — How verbose to be while working; `AVA_AGENT_COMMUNICATION_STYLE` selects `oriented` (default, short progress reports while working) / `concise` (only speak at milestones) / `silent` (work silently, provide a complete summary at the end). **Not an on/off switch**—all three styles share the same channel description ("where output goes"), which is always rendered
- `_output_conciseness_section` — Output conciseness
- `_outcome_reporting_section` — Honest reporting
- `_action_caution_section` — Confirm before irreversible actions
- `_align_before_action_section` — Align on big-picture direction
- `_cross_machine_delegation_section` — One sentence (user-finalized wording, verbatim): when work spans machines, let an agent on the target machine do it rather than reaching across. Toggle `AVA_SYSTEM_PROMPT_CROSS_MACHINE_DELEGATION` (default on); semantic steer only — no API detail, so it cannot go stale.
- `_delegation_check_section` — The 30-second check before taking on work; the prompt's only mandatory-flagged process. The skill-index step (match the task against `# Capabilities`, load the covering skill) sits here rather than in the index itself, because an agent that never reads the index cannot know it is rebuilding one of its own skills. It is dropped and the remaining steps renumbered when this agent renders no Capabilities section at all (`capability_index_is_empty`). The other four steps are the delegation half
- `_file_driven_work_section` — File-driven workflow
- `_temporal_awareness_section` — Time awareness
- `_memory_behavior_section` — Cross-session memory
- `_beyond_task_section` — Candidate next steps after task completion; with fleet context, worthwhile follow-ups are also created as open tasks via `ava.tasks.create()` (with description carrying provenance evidence pointers), while standalone stays as offers to the user
- `_workspace_section` — Workspace description

**Capabilities group** (lives in `_capabilities.py`, registered by `_system_prompt` so the render order stays the reading order):
- `capabilities_section` — The skill + MCP tool index (dynamically generated). `skills_to_inject_into_system_prompt` defaults to `*` = the whole loaded catalog; an explicit list narrows one agent's index. Narrowing hides entries from THIS listing only — `ava.help(ava.skills)` still enumerates the full catalog and an unlisted skill stays reachable by name, which the header says out loud
- Header prose is assembled from whichever halves rendered, so an agent with MCP servers but no skill index is never pointed at a skill listing it does not have
- Each index line is flattened to one line and truncated (`_one_line`): a description is free-form frontmatter from whoever wrote the SKILL.md, including a drop-in under `~/.ava/skills/`

### Keeping the index from going stale (`_capabilities.py:index_drift` + `agent/hooks/capabilities.py`)
- The rendered index is a **snapshot**, built once per window; `ava.skills._names()` under it is an **uncached filesystem scan**. Nothing reconciles them by itself, so a skill installed mid-window would be reachable by name and absent from the listing the delegation check orders the agent to match every task against — until a compaction happened to rebuild the prompt
- `init_context` records the membership it rendered into `state.capabilities.indexed` (see [[../state.ava.okf.md]]). Snapshot taken **before** the render, so a skill landing between the two is named once too many rather than dropped
- A framework-owned `before_llm` hook diffs the live membership against that record each turn and names whatever appeared in one `new_skills` system note, in the index's own line shape; the snapshot advances with the note, so one install produces one note no matter who installed it. Drift is the trigger, not a timer
- `indexed_skills()` is the single definition of membership, so narrowing needs no special case: a configured name that resolved to nothing at build time and resolves now is drift, and a skill outside a narrowed list never becomes drift
- `indexed: None` means no snapshot exists for this window (a checkpoint predating the field). The check then adopts the live catalog silently rather than announcing the whole catalog as new

### SDK Configuration Switches
- `AVA_SYSTEM_PROMPT_*` / `AVA_SDK_*` environment variables control enabling/disabling of each section
- `_disabled_by_sdk_config()` checks these switches

## Entry Points

- `agent/graph/_system_prompt.py:build_system_prompt()` — Build the complete prompt
- `agent/graph/_system_prompt.py:register_system_prompt_section(fn)` — Plugin registration
- `agent/graph/_capabilities.py:capabilities_section()` / `resolve_prompt_skills()` — the `# Capabilities` index and the name→skill resolver it shares with the preloaded-skills note
- `agent/graph/_capabilities.py:indexed_skills()` / `index_drift()` — what the index covers right now, and the diff against a snapshot of it
- `agent/hooks/capabilities.py:register_capabilities_hooks()` — the `before_llm` hook that names skills installed since the index was built
- `agent/graph/_init_context.py:init_context_node()` — caller of `build_system_prompt()`, and where the snapshot is recorded

## Notes

- System prompt + history messages + current turn output = LLM call
- The prompt text is directly aimed at the agent (audience = agent, not developer), must be all English, and must not expose internal implementation names
