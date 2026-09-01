---
type: doc
title: Agent Router Surfaces
description: Agent lifecycle, list-projection, state, and per-agent observability HTTP surfaces served by the gateway.
tags:
- gateway
- agents
---

# Agent Router Surfaces

## Lifecycle and state

`/api/agents/*` covers spawn, terminate, resurrect, restart, compact,
send_message, list, and patch. CRUD and spawn live in `agents.py`; lifecycle
actions live in `agents_lifecycle.py`; message and state reads live in
`agents_state.py`; `agents_forward.py` provides the cross-machine forwarding
helpers.

`/api/cancel` cancels a running turn. `/api/models` exposes available models,
and `/api/agents/{id}/exited` finalizes an agent exit.

## List projections

`GET /api/agents` keeps `scope=all&fields=full` as its compatibility default
for SDK and operations callers. Roster consumers request the SQL-projected
`fields=summary` shape with live or terminated scope; it retains roster state
and response-required notices while omitting checkpoint and probe internals and
raw configuration. `ava agents ls` requests `fields=compact`, the exact
three-field `{agent_id, status, label}` shape. `GET /api/agents/{id}` remains
the full on-demand detail surface.

## Per-agent observability

- `/api/agents/{id}/token-usage` exposes per-model soft and hard compact
  thresholds for the ContextMeter gauge.
- `/api/agents/{id}/context-breakdown` reports checkpoint messages by kind and
  the system prompt by `#` section; character counts are normalized by the
  observed input-token ratio. It is a pure one-read view in
  `gateway/context_breakdown.py`.
