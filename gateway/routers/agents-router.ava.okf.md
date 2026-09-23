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
The former top-level `preset` field is retired (task #4086): a non-null value is refused with a 400 pointing at the overlay key, and a null is tolerated as unset for the compatibility window (the field itself is removed once the window closes).

A fork must keep the source's effective config so its inherited context stays
cache-valid: only ADDITIONS to `skills_to_inject_into_system_prompt` /
`skills_to_expand_at_start` are allowed (supersets — rejected otherwise with
`fork_config_change_not_allowed`); the added skills ride the fork inbound
payload and load at the context tail. A fork without config inherits the
source's overlay + preset verbatim. See
[decision](../../decisions/2026-09-10-preset-in-config-overlay-fork-cache.md).

The POST receipt adds `accepted=true`, `execution_observed=false`, an observed
availability reason, and `observed_at` while retaining `id` for older clients.
The gateway reads the created row after the runner ops launch reply. A 201 says
the launch request and first prompt were accepted; no agent-host turn or first
message claim is synchronously confirmed. The host-down reason comes from the
existing machine status probe, and a recent admission refusal comes from the
agent's durable admission observation. Exact host boot exceptions remain in
machine diagnostics.

## List projections

`GET /api/agents` is a bounded, newest-first directory page with explicit
scope, label/ID search and a keyset cursor. `GET /api/agents/roster` returns
live cards and their minimal ancestor closure in one database snapshot;
unrelated terminated rows receive no per-agent enrichment. Cards carry
attention counts/priority and an open impersonation session number, never notice
bodies. Selected or bookmarked agents
use the independent ID detail endpoint. SDK, CLI and MCP consume the same
page contract; no implicit list-all or field-projection compatibility modes
remain.

Cards and detail expose the same typed `availability` projection independently
of lifecycle `status` and `liveness_state`.

`POST /api/agents/{id}/impersonation/force-expire` accepts the session number
the caller observed. It performs a gateway-local DB transition and wake with
standard gateway authentication, returns `expired` or `not_open`, and returns
404 for an unknown agent. It does not forward to the home runner.

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
