---
type: doc
title: Memory Recall — Passive Memory Recall
description: Conversation-triggered memory injection—automatically performs semantic search on the memory pool when new messages arrive, injecting the most relevant notes into the agent's context. Complements Memory Injection (cold-start index injection).
tags:
- agent-core
- memory
- context
- removable
---

# Memory Recall — Passive Memory Recall

## What it is

**Passive Memory Recall** is a mechanism in the agent graph's `claim` stage: when new inbound messages wake the agent, the system automatically performs a semantic search over the last 6 dialogue messages, retrieves the top-100 candidates from the shared memory pool, has a small filter model keep the relevant ones, and injects at most 3 as system annotations into the agent's context.

The agent **does not need to actively call `ava.memory.search()`**—relevant memories surface automatically.

## Difference from Memory Injection

| Mechanism | Trigger | Injected content | Quantity |
|---|---|---|---|
| **Memory Injection** (`notes.py`, laid down by the `init_context` node) | Whenever the context window is established (cold start / after compaction) | MEMORY.md index (shared pool + per-agent) | Fixed (full index text) |
| **Memory Recall** (`_memory_recall.py` + `_memory_filter.py`) | Every new inbound message (user / agent / schedule / system — watcher & shell wake-ups skipped) | Memory pool notes the filter listed as worth seeing | Retrieve 100 → inject ≤3, or none |

The two are complementary: injection gives the agent a "memory directory", while recall pushes relevant content automatically during conversation.

## Mechanism Details

- **Trigger condition**: `passive_memory_recall_enabled` (default **on**; a cluster opts out by pinning `AVA_PASSIVE_MEMORY_RECALL=false` in its `.env` — `cluster-pinned` scope, so it applies cluster-wide), and memory index available. This node owns the recall settings' documented defaults; the authority they track is `shared/config/agent.py`
- **Query construction**: Takes the last 6 "real dialogue" messages (agent replies + inbound chats), excludes system heartbeats/lifecycle markers/previous recall injections
- **Trigger gate**: fires on inbound from a real source — user chat, a peer agent (`agent:`), a scheduled turn (`schedule:`), or a system notice — and skips machine-originated wake-ups (`watcher:` / `shell:` prefixes; `tail_has_recallable_inbound` in `agent/messages.py`)
- **Search**: retrieves `memory_recall_retrieve_k` (100) candidates — wide top-100 so the filter has real candidates to judge; injection stays capped at `memory_recall_inject_k`
- **Filter**: `memory_recall_filter_model` (deepseek-v4-flash) judges path + description + `type/<x>` tag against the conversation and lists the notes worth the agent's attention, at most `memory_recall_inject_k` (3), or none. The prompt is relaxed: when unsure it lists rather than rejects — the agent decides whether to read. The model is built with reasoning pinned off (see `agent/graph/_memory_filter.py` — the registry's `max` default made every call time out); flash is the default per user ruling (task #595) — the relaxed prompt + top-100 retrieval made flash match pro on the recall cases, so the cheap model stays. Error / timeout / unreadable reply injects nothing, never the unfiltered top-k the filter exists to remove
- **Dedup**: Runs **after** the filter, on what it judged relevant — the filter sees the full candidate set (dropping already-injected notes first would leave a similar second message with its best matches pre-removed, injecting unrelated notes instead). Notes already injected this session are not injected again; if everything the filter kept is already injected, nothing is
- **Degradation**: Any failed search skips the turn instead of failing it — the hook runs inside `before_llm`, so an escaping exception ends the agent process rather than the recall. Two levels: an outage the gateway names in the wire contract (`IndexerUnavailable` / `GatewayUnavailable`) logs at debug, because a restart or a stalled embedder clears itself; an error **status** (`httpx.HTTPStatusError` — a body carrying no wire `reason`, e.g. a bare 500) logs at error under `event=passive_recall`, because the gateway failed in a way it does not model. Infrastructure only: a programming error in the call path still propagates

## Injection Format

```markdown
Memory notes related to the current conversation, retrieved automatically.
These point into your note pool at ava.memory.PATH -- read a path in full when it looks relevant.

- path/to/note.md: description from frontmatter
```

## Key Dependencies

- [[ava_builtins/plugins/ava_memory/memory-api.ava.okf.md]] — Memory pool SDK
- [[services/gateway_side/memory_indexer/memory-indexer.ava.okf.md]] — Vector index service
- [[shared/lm/lm.ava.okf.md]] — Embedding model for semantic search
- [[agent/graph/context-notes.ava.okf.md]] — the standing head recall notes are injected beside
- [[agent/graph/context-window.ava.okf.md]] — injection is constrained by context window capacity
