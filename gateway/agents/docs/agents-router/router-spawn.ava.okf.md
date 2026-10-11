---
type: doc
title: Agent Spawn Boundary
description: Preset and fork configuration, durable creation acceptance and launch retries.
tags:
- gateway
- agents
---

# Spawn Boundary: Presets and the Fork Config Rule

`POST /api/keyed/v1/agents` resolves a preset named inside the config overlay
(`config["preset"]`) at the spawn boundary: the preset's stored config is the
base and the explicit fields win per key; the row stores the RESOLVED overlay
plus `agents_meta.preset_name` for display (diff semantics in the inspector).
Spawn requests reject unknown top-level fields with 422, including `preset`
even when null. Preset selection belongs only in `config["preset"]`.

A fork must keep the source's effective config so its inherited context stays
cache-valid: only ADDITIONS to `skills_to_inject_into_system_prompt` /
`skills_to_expand_at_start` are allowed (supersets — rejected otherwise with
`fork_config_change_not_allowed`); the added skills ride the fork inbound
payload and load at the context tail. A fork without config inherits the
source's overlay + preset verbatim. See
[decision](../../../../docs/decisions/runtime/config/2026-09-10-preset-in-config-overlay-fork-cache.md).

The POST receipt adds `accepted=true`, `execution_observed=false`, an observed
availability reason, and `observed_at`; `id` identifies the committed agent.
The gateway commits the row, fork marker if present, and first prompt in one
transaction before forwarding a `spawn-launch` op. The runner validates and
publishes a repeatable wake; it does not insert a new prompt or terminate the
row on launch failure. The gateway reads the created row after the ops reply.
A 201 says the launch request was accepted and the first prompt is pending;
no agent-host turn or first
message claim is synchronously confirmed. The host-down reason comes from the
existing machine status probe, and a recent admission refusal comes from the
agent's durable admission observation. Exact host boot exceptions remain in
machine diagnostics. If the post-launch availability read fails or the created
row is unavailable, the receipt still returns 201 with
`reason=unknown` and a fresh `observed_at` because row creation and the launch
reply have already succeeded.

If the forward fails after creation, the gateway conditionally records a typed
launch failure on the row and responds 502 `agent_launch_failed` with
`agent_id`, actual `state.status`, projected availability, and a legal
`retry_launch_path` pointing to the guarded route. The browser selects that
committed agent. `POST /api/keyed/v1/agents/{id}/retry-launch` requires an explicit
observed prior attempt and principal-scoped key. It fixes one new attempt in an
immutable receipt and reconciles only that attempt's wake; replay cannot rotate
another attempt or insert another inbound. The old unversioned retry route is
removed. See [[gateway/agents/docs/launch-retry.ava.okf.md]]. Admission racing a failed
forward wins and produces an accepted receipt. A failure-state DB read/write
outage still returns the committed ID with an unknown state. Every creation
carries an `Idempotency-Key` and `Idempotency-Scope: principal-v1`, which binds
the key to the authenticated principal namespace. The immutable request hash and
key commit as an `agent_creation_snapshots` record with the agent row, the fork
marker and first prompt. Concurrent retries serialize the same birth, return its
existing ID, and recover a still-unadmitted launch using its committed attempt
and config. Receipt lookup precedes mutable model/preset/fork validation.
Reusing the key with changed request data returns 409. Creation snapshots are
never pruned. A replay never
launches an agent that has since been admitted or terminated. The SDK supplies
one key per creation call and reuses it for connect-family retries. Automatic
retry of an ambiguous outcome requires proven gateway capability and remains
disabled because an older gateway may ignore the key.
The runner accepts only `spawn-launch`, with a required UUID
`launch_attempt_id` matching the committed row and local placement. Runner
launch payloads reject `prompt`, `prompt_source`, `label`, and unknown fields.


Guarded creation: [[gateway/agents/docs/guarded-creation.ava.okf.md]].
