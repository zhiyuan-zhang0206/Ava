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
helpers. The billing batch-recovery entry is `POST /api/agents/resurrect-billing`
(a read-only preview unless the body sets `execute`; orchestration in
`ops/billing_recovery.py`, per-agent dispatch via the versioned
`resurrect-billing-v1` home action).

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
[decision](../../decisions/2026-09-10-preset-in-config-overlay-fork-cache.md).

## List projections

`GET /api/agents` is a bounded, newest-first directory page with explicit
scope, label/ID search and a keyset cursor. `GET /api/agents/roster` returns
live cards and their minimal ancestor closure in one database snapshot;
unrelated terminated rows receive no per-agent enrichment. Cards carry
attention counts/priority, never notice bodies. Selected or bookmarked agents
use the independent ID detail endpoint. SDK, CLI and MCP consume the same
page contract; no implicit list-all or field-projection compatibility modes
remain.

## Per-agent observability

- `/api/agents/{id}/born-chain` returns the immutable birth chain above an
  agent (nearest ancestor first, each row carrying label / status / machine /
  depth) — one recursive `agents_meta.born_spawner` walk with none of
  `/neighbors`' tie graph. It is the read the inherited-memory context note
  resolves at every window establishment (`plugins/ava_memory/inherit.py`), so
  it is kept deliberately light: no neighbor ranking, no Loki live tail.
- `/api/agents/{id}/token-usage` exposes per-model soft and hard compact
  thresholds for the ContextMeter gauge.
- `/api/agents/{id}/context-breakdown` reports checkpoint messages by kind and
  the system prompt by `#` section; character counts are normalized by the
  observed input-token ratio. It is a pure one-read view in
  `gateway/context_breakdown.py`.
