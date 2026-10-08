---
type: doc
title: System Prompt — System Prompt
description: "The system prompt built once per context window. Constructed by `build_system_prompt(extensions, slices, agent_id=...)` in order: base guidance → SDK overview → behavior conventions → capability index → plugin sections."
tags: []
---

# System Prompt — System Prompt

## What it is

The system prompt carried in every LLM call, built **once per context window** — `init_context` renders it when it lays the standing head down (an agent's first wake, and the turn after each compaction), never per turn. Constructed by `build_system_prompt()` in registration order: base guidance → SDK overview → behavior conventions → capability index.

## Core Mechanism

### build_system_prompt (`agent/graph/prompt/system_prompt.py:build_system_prompt`)
- The base is the `_BASE_SYSTEM_PROMPT` constant in `agent/graph/prompt/_base_prompt.py` (the `{_AVA_OVERVIEW}` placeholder injects the SDK overview)—**it never reads `AGENTS.md` at runtime**
- Appends SDK documentation (the output of `ava.help(ava)`)
- Runs `FRAMEWORK_SECTIONS` in fixed order, then the registry's plugin sections
- Injects the skill and MCP server index — **once**. `# Capabilities` is the sole index; the expanded SDK reference's `*` skips `ava.skills` / `ava.mcps` (`_CAPABILITY_SURFACES`) so it renders call contracts only, never a second capability listing

### Plugin Declaration (`PluginContributions.system_prompt_sections`)
- A plugin declares sections in `contribute()`; the `ExtensionRegistry` reaches this build as `AvaContext.extensions` ([[okf/plugins/declared-contributions.ava.okf.md]])
- Signature `(slices: AgentSlices) -> str`; `""` means no contribution
- Declaration order (plugins by name) is priority
- Framework's built-in sections are grouped: SDK detail → Conversation → Conduct → Capabilities

### Framework Built-in Sections

**Conduct group**:
- `_prefer_sdk_section` — "Prefer SDK"
- `_codeact_section` (in `agent/graph/prompt/_codeact.py`, registered by `system_prompt`) — "CodeAct — batch work into fewer calls": batch known operations when no intermediate review is needed; split for decisions, approval or execution/output limits. Toggle `AVA_SYSTEM_PROMPT_CODEACT` (default on; explicit cluster settings and per-agent overlays can disable it).
- `_keep_it_simple_section` — "Keep It Simple"
- `conversation.user_reply_section` — Owns ordinary assistant-text reply routing, including text alongside tools; answer humans or reply before investigation, including resumed handoff requests, with every style. Continue investigating only concrete remaining questions; no courtesy replies for wakes or peers.
- `_communication_style_section` — How verbose to be while working; `AVA_AGENT_COMMUNICATION_STYLE` selects `off` (default; section omitted entirely) / `oriented` (short progress reports while working) / `concise` (only speak at milestones) / `silent` (work silently, provide a complete summary at the end)
- `_output_conciseness_section` — Output conciseness
- `_outcome_reporting_section` — Honest reporting
- `_action_caution_section` — Confirm before irreversible actions
- `_align_before_action_section` — Resolve material outcome, cost, autonomy, and authority choices; respect existing consent rather than reapprove settled work after planning
- `_cross_machine_delegation_section` — One sentence (user-finalized wording, verbatim): when work spans machines, let an agent on the target machine do it rather than reaching across. Toggle `AVA_SYSTEM_PROMPT_CROSS_MACHINE_DELEGATION` (default on); semantic steer only — no API detail, so it cannot go stale.
- `_delegation_check_section` — Consult the capability index and load `ava-workflow` when available for non-trivial or consequential work. Select methods and peer collaboration by need, without forcing interviews, plans, supervision, or delegation. The index step is omitted and steps renumbered when the index is empty.
- `_file_driven_work_section` — File-driven workflow
- `_long_running_operation_section` — Lifecycle and cost principles without fleet; the long-running-agent skill supplies procedures.
- `_temporal_awareness_section` — Time awareness, including the `ava-workflow.capability-timescale` skill invoke at scheduling, estimation, and feasibility-judgment moments
- `ava_memory.memory_discipline_section` — Cross-session durable-knowledge behavior
- `_invest_in_the_future_section` — Framework's one cross-domain future-signal rule; `AVA_SYSTEM_PROMPT_INVEST_FUTURE` defaults on and selects the smallest closing action for a signal that could improve later work
- `_workspace_section` — Workspace description (id-free for fork safety; the concrete path is stated by the agent-ID context note)

**Capabilities group** (lives in `capabilities.py`, registered by `system_prompt` so the render order stays the reading order):
- `capabilities_section` — The skill + MCP index. `*` selects the full catalog, but folds descendants under real entry skills with counts and inspection paths. Namespace-only folders retain leaves. Explicit lists render every selected entry; all skills stay reachable through `ava.help(ava.skills)`.
- Header prose names only the halves actually rendered.
- Descriptions flatten to one line, capped at 300 characters. The initial index has no total cap.

### Keeping the index from going stale (`agent/graph/prompt/capabilities.py:index_drift` + `agent/hooks/capabilities.py`)
- The rendered index is a **snapshot**, built once per window; `ava.skills.names()` under it is an **uncached filesystem scan**. Nothing reconciles them by itself, so a skill installed mid-window would be reachable by name and absent from the listing the delegation check orders the agent to match every task against — until a compaction happened to rebuild the prompt
- `init_context` records the full selected catalog membership (including folded descendants) into `state.capabilities.indexed` (see [[../../docs/state.ava.okf.md]]). Snapshot taken **before** the render, so a skill landing between the two is named once too many rather than dropped
- A framework-owned `before_llm` hook diffs the live membership against that record each turn and names whatever appeared in one `new_skills` system note, in the index's own line shape; the snapshot advances with the note, so one install produces one note no matter who installed it. Drift is the trigger, not a timer
- `indexed_skills()` is the single definition of membership, so narrowing needs no special case: a configured name that resolved to nothing at build time and resolves now is drift, and a skill outside a narrowed list never becomes drift
- Unresolved configured names warn on each resolution.
- `indexed: None` means no snapshot exists for this window (a checkpoint predating the field). The check then adopts the live catalog silently rather than announcing the whole catalog as new

## Entry Points

- `agent/graph/prompt/system_prompt.py:build_system_prompt(extensions, slices)` — Build the complete prompt
- `base/packages/plugins/extensions.py:PluginContributions` — what a plugin's `contribute()` declares
- `agent/graph/prompt/capabilities.py:capabilities_section(slices)` / `resolve_prompt_skills()` — the `# Capabilities` index and the name→skill resolver it shares with the preloaded-skills note
- `agent/graph/prompt/capabilities.py:indexed_skills(prompt)` / `index_drift(known, prompt)` — what the index covers right now, and the diff against a snapshot of it
- `agent/hooks/capabilities.py:_newly_installed_skills` — the `before_llm` hook that names skills installed since the index was built
- `agent/graph/_init_context.py:init_context_node()` — caller of `build_system_prompt()`, and where the snapshot is recorded

## Notes

- System prompt + history messages + current turn output = LLM call
- The prompt text is directly aimed at the agent (audience = agent, not developer), must be all English, and must not expose internal implementation names
