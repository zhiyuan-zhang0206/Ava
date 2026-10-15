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

**Passive Memory Recall** is a mechanism in the agent graph's `claim` stage: when new inbound messages wake the agent, the system automatically performs a semantic search over the last 6 dialogue messages, searches the shared memory pool through the gateway (vector retrieval, then a decision model scoring each candidate chunk's relevance), and injects the notes scoring at or above a threshold — at most 3 — as a system annotation into the agent's context.

The agent **does not need to actively call `ava.memory.search()`**—relevant memories surface automatically. Each injected note presents path, score, frontmatter description and the passage that matched — `ava.memory.search`'s fields minus tags (the injected note is a pointer, not a tag list). Description is empty when absent, never synthesized from title/body.

## Difference from Memory Injection

| Mechanism | Trigger | Injected content | Quantity |
|---|---|---|---|
| **Memory Injection** (`notes.py`, laid down by the `init_context` node) | Whenever the context window is established (cold start / after compaction) | MEMORY.md index (shared pool + per-agent) | Fixed (full index text) |
| **Memory Recall** (`memory_recall.py`) | Every new inbound message (user / agent / schedule / system — watcher & shell wake-ups skipped) | Memory pool notes whose relevance score reaches the threshold, with the matched passage | Score ≥ `memory_recall_inject_threshold` → inject ≤ `memory_recall_inject_max` (3), or none |

The two are complementary: injection gives the agent a "memory directory", while recall pushes relevant content automatically during conversation.

## Mechanism Details

- **Trigger condition**: `passive_memory_recall_enabled` (default **on**; a cluster opts out by pinning `AVA_PASSIVE_MEMORY_RECALL=false` in its `.env` — `cluster-pinned` scope, so it applies cluster-wide), and memory index available. This node owns the recall settings' documented defaults; the authority they track is `base/config/domains/agent/settings.py`
- **Query construction**: Takes the last 6 "real dialogue" messages (agent replies + inbound chats), excludes system heartbeats/lifecycle markers/previous recall injections (`agent/graph/recall/conversation_query.py:build_conversation_query`, shared with skill recall, [[agent/graph/docs/skill-recall.ava.okf.md]])
- **Trigger gate**: fires on inbound from a real source — user chat, a peer agent (`agent:`), a scheduled turn (`schedule:`), or a system notice — and skips machine-originated wake-ups (`watcher:` / `shell:` prefixes; `tail_has_recallable_inbound` in `agent/messages/__init__.py`)
- **Search**: the same gateway search `ava.memory.search` runs (see [[ava_builtins/plugins/ava_memory/docs/memory-api.ava.okf.md]]): vector retrieval of the best chunk of the closest `memory_rerank_candidates` (30) notes, then the decision model `memory_rerank_model` scores each chunk's relevance to the query. Recall holds no model call and no relevance judgement of its own, and no text is generated on this path. It asks for exactly `memory_recall_inject_max` results and sends the context window's already-injected paths as the request's `exclude` list, so the gateway drops them before ranking: they take no result slot and cost no ranking request, and one pass issues the same number of decision requests (at most `memory_rerank_candidates`) at any session age. (Until 2026-10 recall asked for the cap plus the injected count, up to 100, and the gateway ranked that many, so cost grew with the session.) The request also carries `memory_recall_deadline_seconds` as its deadline, so the gateway stops ranking when the pass gives up, and is sent with one attempt: the blocking call cannot be interrupted from the agent, and a retry would start a second search after the pass moved on. `memory_recall_inject_max` is at most 100 (the search's result limit), and a context window that has had more than 800 distinct notes injected (the `exclude` limit) gets no further recall: the gateway refuses the list, which logs at error. The record is per window: a compaction summarizes the injected notes away, so the next pass starts from an empty record and may recall the same notes again (see `MemoryState.window` in [[agent/docs/state.ava.okf.md]])
- **Threshold**: injects the results scoring at least `memory_recall_inject_threshold` (default 0.80, calibrated by the 2026-10-11 benchmark for `jev-1.13.0` with the ranking prompts in `rerank.py`; a different model or prompt wording needs recalibrating), best first, at most `memory_recall_inject_max` (3). A turn where nothing reaches the threshold injects nothing; vector order alone never injects anything
- **Dedup**: Done by the search, not after it: notes already injected in this context window are excluded before ranking, so the cap fills with the next-best candidates. If every candidate is already injected, nothing is
- **Degradation**: Any failed search skips the turn instead of failing it — the hook runs inside `before_llm`, so an escaping exception ends the agent process rather than the recall. Three levels: an outage the gateway names in the wire contract (`IndexerUnavailable` / `GatewayUnavailable`, which includes a failed decision-model call — the gateway logs that one at warning) logs at debug, because a restart or a stalled embedder clears itself; an error **status** (`httpx.HTTPStatusError` — a body carrying no wire `reason`, e.g. a bare 500) logs at error under `event=passive_recall`, because the gateway failed in a way it does not model; and the whole pass runs under `memory_recall_deadline_seconds` (default 5.0, covering the search request including the gateway's ranking) — on expiry the turn proceeds with no recall this turn (logged under `event=passive_recall`) and the next inbound turn retries, so a congested gateway (a fleet wake queueing searches) degrades to a bounded skip instead of stalling the first LLM call. A congested search gate now answers 503 in ~1s (`AVA_MEMORY_SEARCH_ACQUIRE_TIMEOUT_SECONDS`, default 1.0), so in practice the pass degrades within about a second rather than burning the whole recall deadline on the queue. Infrastructure only: a programming error in the call path still propagates

## Injection Format

```markdown
Memory notes related to the current conversation, retrieved automatically, each with the passage that matched and its relevance score.
These point into your note pool at ava.memory.PATH -- read a path in full when it looks relevant.

- path/to/note.md (score 0.83): description from frontmatter
  > the passage of the note that matched
```

Telemetry: each completed pass logs `event=passive_recall` with `search_ms` and `injected`; the `ava_memory` Grafana panels (runs, empty ratio, search latency, notes injected, deadline skips) read those.

## Key Dependencies

- [[ava_builtins/plugins/ava_memory/docs/memory-api.ava.okf.md]] — Memory pool SDK
- [[services/docs/gateway_side/memory_indexer/memory-indexer.ava.okf.md]] — Vector index service
- [[base/lm/docs/lm/lm.ava.okf.md]] — Embedding model for semantic search
- [[agent/graph/docs/context-notes/context-notes.ava.okf.md]] — the standing head recall notes are injected beside
- [[agent/graph/docs/context-window/context-window.ava.okf.md]] — injection is constrained by context window capacity
