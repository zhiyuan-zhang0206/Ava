---
type: doc
title: Gateway terminology
description: "The gateway domain's authoritative vocabulary: the Gateway as one FastAPI app after cutover, and the Client as the HTTP consumers of /api/*."
tags: [gateway]
---

# Gateway terminology

> This is the authoritative terminology for the gateway domain.

- **Gateway** — one FastAPI app, one port; after cutover more than an adapter, also the cluster orchestrator (`gateway/cluster/router.py`; `ops/cluster/rpc.py` for cross-machine RPC to agent-ops `/ops`). Keeps the old name — `gateway` is the identifier for 170+ files, renaming churn outweighs benefit. Three "not's": **not the lifecycle owner** (`agents_meta` is the truth; agents spawn/terminate/heartbeat via DB + the native supervisor; release decisions run outside the gateway too, a retained native operation), **does not route inter-agent messages** (`inbound_messages` is the bus; gateway writes to it via `ops/lifecycle/delivery.py:deliver_chat_inbound`, never routes between agents), **stateless** (restart loses no state). Chat writes may carry a `client_message_id` (unique constraint with the inbound); `POST .../messages/reconcile` returns the stable `inbound_id` after an ambiguous response and replays the pending wake/resurrection tail — a gateway death cannot duplicate chat.
- **Client** — HTTP consumers of the gateway (more than one): Next.js browser frontend + agent SDK on agent-runner, both directly connect to `/api/*` over private network. Client **does not** talk to agents directly — everything goes through gateway HTTP.

## Relationship to Other Nodes

- [[gateway/docs/gateway.meiva.okf.md|Gateway]] — the domain node this vocabulary serves.
