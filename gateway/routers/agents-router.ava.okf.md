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

## Spawn boundary: presets and the fork config rule

`POST /api/agents` resolves a preset named inside the config overlay
(`config["preset"]`) at the spawn boundary: the preset's stored config is the
base and the explicit fields win per key; the row stores the RESOLVED overlay
plus `agents_meta.preset_name` for display (diff semantics in the inspector).
The legacy top-level `preset` field is a deprecated alias for the overlay key.

A fork must keep the source's effective config so its inherited context stays
cache-valid: only ADDITIONS to `skills_to_inject_into_system_prompt` /
`skills_to_expand_at_start` are allowed (supersets — rejected otherwise with
`fork_config_change_not_allowed`); the added skills ride the fork inbound
payload and load at the context tail. A fork without config inherits the
source's overlay + preset verbatim. See
[[decisions/2026-09-10-preset-in-config-overlay-fork-cache.md]].

## List projections

`GET /api/agents` keeps `scope=all&fields=full` as its compatibility default
for SDK and operations callers. Roster consumers request the SQL-projected
`fields=summary` shape with live or terminated scope; it retains roster state
and response-required notices while omitting checkpoint and probe internals and
raw configuration. `ava agents ls` requests that authenticated summary projection
and renders only `agent_id`, `status`, `machine`, and `label`; runner-local
workspace paths do not cross this boundary. `fields=compact` remains an available
legacy narrow projection. `GET /api/agents/{id}` remains the full on-demand detail
surface.

## Per-agent observability

- `/api/agents/{id}/token-usage` exposes per-model soft and hard compact
  thresholds for the ContextMeter gauge.
- `/api/agents/{id}/context-breakdown` reports checkpoint messages by kind and
  the system prompt by `#` section; character counts are normalized by the
  observed input-token ratio. It is a pure one-read view in
  `gateway/context_breakdown.py`.
