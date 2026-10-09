---
type: doc
title: Agent Spawn Boundary
description: Preset and fork configuration, durable creation acceptance and launch retries.
tags:
- gateway
- agents
---

# Spawn Boundary: Presets and the Fork Config Rule

`POST /api/agents` resolves a preset named inside the config overlay
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
transaction before forwarding a `spawn-launch-v2` op. The runner validates and
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
`retry_launch_path`. The browser selects that agent and offers Retry launch.
`POST /api/agents/{id}/retry-launch` rotates `last_launch_attempt_id`, reuses
the stored machine/config/birth stamp, and forwards the same identity without
another inbound. Within one attempt, `spawn-launch-v2` keeps its canonical RPC
dedupe key; a new attempt is a repeatable wake. Admission racing a failed
forward wins and produces an accepted receipt. A failure-state DB read/write
outage still returns the committed ID with an unknown state. A caller may supply `Idempotency-Key` to `POST /api/agents`. The immutable
request hash and key commit on the agent row with the fork marker and first
prompt. Concurrent retries serialize the same birth, return its existing ID,
and recover a still-unadmitted launch using its committed attempt and config.
Receipt lookup precedes mutable model/preset/fork validation. Reusing the key
with changed request data returns 409; a keyless create still creates a new
identity. `Idempotency-Scope: principal-v1` uses the existing authenticated
principal namespace; legacy raw keys remain global. Creation identities remain
with the agent row and are not pruned with response caches. A replay never
launches an agent that has since been admitted or terminated. The SDK supplies
one key per creation call and reuses it for connect-family retries. Automatic
retry of an ambiguous outcome requires proven gateway capability and remains
disabled because an older gateway may ignore the key.
The runner accepts only `spawn-launch-v2`, with a required UUID
`launch_attempt_id` matching the committed row and local placement. Runner
launch payloads reject `prompt`, `prompt_source`, `label`, and unknown fields.
`spawn-launch` is outside the RPC vocabulary and fails before machine lookup,
maintenance admission or dedupe.


Guarded creation: [[gateway/agents/docs/guarded-creation.ava.okf.md]].
