---
type: doc
title: Standing Context — What Sits Behind the System Prompt
description: The standing head of an agent's context window — the SystemMessage plus the ordered context notes behind it, who owns them, and how memory notes reach them. Laid down by the `init_context` node whenever a window is established.
tags: []
---

# Standing Context — What Sits Behind the System Prompt

## What It Is

The part of an agent's context window that is not conversation: the SystemMessage
and the ordered context notes behind it. It is established twice in an agent's
life — first wake, and the turn after any compaction — and by one owner.

## Core Mechanisms

### Standing Context Notes (`_context_notes.py`, laid down by the `init_context` node)
- One ordered registry, one owner: `init_context` runs before `claim` and lays down the head — SystemMessage + every registered note — whenever `messages` is empty. A compaction empties the window, parks the tail in `state.context_reset`, and routes back here, so cold start and post-compact are one code path.
- Framework notes: agent id (`on_fork`), exec timeout, preloaded skills. Plugins append at load; `clear_plugin_registrations` truncates to `_FRAMEWORK_NOTE_COUNT`. `on_fork` = the subset grafted onto a fork's inherited history (`fork_notes`).

### Memory Injection (`plugins/ava_memory/notes.py`, registered by the plugin)
- Both stores belong to `ava_memory`; disabling it removes stores, SDK surface, notes, and discipline section together.
- Two sources, independently switchable (strippable):
  - **Shared MEMORY.md** (memory pool root `ava.memory.PATH`, pointer index, visible to all agents) — `settings.agent.memory_index_inject_enabled`
  - **Per-agent memory** (workspace `memory/` dir: `memory/MEMORY.md` index, entries as sibling files read on demand) — `settings.agent.memory_per_agent_inject_enabled`. Not injected on fork; empty index created if missing; a legacy single-file `<workspace>/MEMORY.md` migrates in on first injection, never overwriting. **Only the index is injected**; past `memory_per_agent_index_max_lines` (default 200, 0=off) a maintenance reminder is appended — no truncation.
- The write discipline both stores share (criteria, mandatory-write triggers, the `type/*` vocabulary, weighing a memory against self-verifying vs asserted sources) is a **system prompt section** owned by the same plugin — fixed text, where an index is not.

### Passive Recall (`_memory_recall.py` + `_memory_filter.py`)
- Content-driven recall on **new inbound wake-up turns** (not agent-initiated search). The trigger gate skips machine-originated wake-ups (`watcher:` / `shell:` prefixes) and fires on user chat, peer-agent (`agent:`), scheduled (`schedule:`), and system inbound. **Two stages**: retrieve `memory_recall_retrieve_k` (100) by semantic search, then `memory_recall_filter_model` (deepseek-v4-flash) lists the notes worth the agent's attention and at most `memory_recall_inject_k` (3) are injected. Vector search always returns its top-k, so the filter exists to keep weak matches out of context — its bias is to list when unsure (the agent reads a listed note only when it looks useful), and an unfiltered recall injects notes on every turn regardless of fit.
- The filter judges paths, descriptions, and the `type/<x>` tag only, never bodies. Its explicit rule: `type/user` / `type/project` notes match on subject matter, never on sharing a word with the question.
- The filter's model is built with reasoning pinned off (`build_chat_model(..., reasoning_effort=ReasoningEffort.NONE)`) — the registry's deepseek `max` default made every call exceed the 20s bound and time out.
- Never fatal: filter error / timeout / unparseable reply → inject nothing (never the unfiltered top-k the filter exists to reject); an invented path is dropped. Search failure (index unavailable, or the gateway answering with an error status) → no-op, logged; the hook runs in `before_llm`, so an exception escaping it would end the agent process, not just the recall. Same-session dedup happens **after** the filter, on what it judged relevant — the filter sees the full candidate set so a second message close to the first cannot have its best matches pre-removed.
- `settings.agent.passive_memory_recall_enabled` gates the stage; `memory_recall_filter_enabled` (off) passes the unfiltered top-k through. Their defaults and the cluster opt-out are stated in [[ava_builtins/plugins/ava_memory/memory-recall.ava.okf.md]], not here.

### Context Structure
- System prompt (fixed, included in every LLM call)
- Standing context notes (agent id / exec timeout / preloaded skills / the two memory indexes) laid down behind the system prompt whenever the window is established
- Message history (user messages + agent replies + tool call results)
- Current turn input


## Key Dependencies

- [[context-window.ava.okf.md]] — compaction is what makes the head need re-establishing
- [[system-prompt.ava.okf.md]] — the head's first message, and where the memory discipline lives as a section
- [[ava_builtins/plugins/ava_memory/memory-recall.ava.okf.md]] — passive recall in depth

## Entry Points
- `agent/graph/_init_context.py` — the node that lays down the head
- `agent/graph/_context_notes.py` — the ordered note registry + the framework's notes
- `ava_builtins/plugins/ava_memory/notes.py` — the two memory index notes
- `agent/graph/_memory_recall.py` + `_memory_filter.py` — passive recall and its filter
