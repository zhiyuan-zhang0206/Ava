---
type: doc
title: Agent inbox
description: Inbound-message inserts, the Redis wake, live-agent predicate and the pending/anchor reads over the agent tables.
tags: [base, agent-lifecycle]
---

# Agent inbox

## What it is

`base/agents/inbox/` owns the writes and reads over the agent inbox that used to sit in
`base/db/__init__.py`. `base/db` keeps the connection policy, the `Database` handle and the
agent-table primitives (`create_agent`, `agent_exists`, `list_agents`, `fetch_one`); everything
that needs agent contracts, the audit log, the event bus or the cluster wake keys lives here, so
the database layer imports none of them.

## Core responsibilities

- `insert.py` — `insert_inbound_message` (self-committing: row, audit fact, post-commit emit and
  wake), `insert_inbound_message_in_transaction` (the caller owns commit and wake),
  `insert_spawn_prompt_in_transaction` / `announce_spawn_prompt` and `emit_prepared_event`. The
  lineage audit fact (`send_message`, `terminate`, `restart`, `cancel`, `resurrect`, `fork`, …) is
  recorded in the same transaction as the row.
- `wake.py` — `publish_inbound_wake`: best-effort Redis publish plus the durable wake-key
  breadcrumb. It returns whether the wake reached Redis; callers that meter delivery must read it.
- `compact.py` — `insert_compact_request_inbound`, the native compact envelope.
- `live.py` — `ALIVE_STATUSES` / `ALIVE_SQL` / `agent_is_alive` (the one alive predicate: status
  plus an unexpired lease), `list_live_agent_ids` and `signal_live_agents_restart` (the fleet
  update's quiesce signal).
- `rows.py` — `InboundRow` / `ChatAnchor`, `list_pending_inbounds`, `list_chat_anchors` and
  `NOTICE_FYI_TTL_DAYS`, the open-feed lifetime of FYI notices.

## Key dependencies

- [[base/agents/messages/docs/inbound-provenance.ava.okf.md]] — provenance facts stored with an
  inbound row.
- `base/agents/impersonation` — `record_central_event` for the borrowed-identity event log, and the
  relay liveness alert `publish_inbound_wake` runs first. `base.agents.impersonation` imports
  `wake.py` at load time, so that one import stays inside the function.

## Entry points

- `base/agents/inbox/insert.py:insert_inbound_message`
- `base/agents/impersonation/wake.py:publish_inbound_wake`
- `base/agents/inbox/live.py:signal_live_agents_restart`
