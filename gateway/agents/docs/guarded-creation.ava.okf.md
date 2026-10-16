---
type: doc
title: Guarded Agent Creation
description: Principal-bound immutable agent and fork acceptance with original-attempt recovery.
tags:
- gateway
- agents
---

# Guarded Agent Creation

`POST /api/keyed/v1/agents` accepts creation and forks through the existing birth
transaction and launch owner. Every request requires a valid `Idempotency-Key`
(1–128 characters), the exact `Idempotency-Scope: principal-v1` header, and a
verified authenticated principal. Missing or invalid admission, an unverified
principal are rejected before birth or launch effects. Existing
authentication still applies to replay; revoked credentials cannot retrieve a
receipt.

The receipt identity includes the principal, POST method, and this versioned
logical path. Identical keyed requests replay the committed birth; changed
request data returns 409. The birth owner commits an immutable
`agent_creation_snapshots` record with the agent row, first prompt and audit
event in one transaction. Its manifest retains
the original agent ID, machine, config overlay, birth config and launch attempt
UUID. The request digest still uses the existing parsed `SpawnAgentRequest`;
this does not change the namespace or adopt guarded drafts' raw semantic identity.
Acceptance does not prove native execution.

Replay reads this retained birth before mutable placement and model checks. Only the original attempt, still idling, never admitted and on its
original machine, can receive the existing launch wake. A deliberate launch
retry, changed placement, admission, termination or deleted metadata returns
the original historical acceptance without forwarding a launch. Replay never
rotates an attempt, restores operator config or creates another first prompt.
The retained snapshot has no target FK or automatic expiry. This is the only
agent creation route: the browser, IM bridge, CLI, scripts, SDK and examples all
use it, and every other creation entry (guarded drafts, the MCP tool and task
assignment) records its birth through the same snapshot owner.

The SDK requires a caller key for every creation or fork and always uses this
path. The former `require_idempotency` mode flag has been removed. Forks use the
existing birth owner: source checkpoint resolution occurs for first acceptance,
then checkpoint copying, the fork marker, first prompt, immutable receipt and
audit commit together. Same-key replay reads the retained child first and does
not resolve or copy a later source checkpoint. See the SDK owner at
`ava/agents/docs/strong-creation.ava.okf.md`.

Gateway MCP clients use `spawn_agent_guarded_v1` for retained original
birth recovery. The versioned tool name admits each write on its serving
gateway before effects and has a distinct authenticated MCP-client namespace;
unsupported servers reject it without legacy fallback. See the
[MCP endpoint owner](../../mcp_server/docs/mcp-endpoint/mcp-endpoint.ava.okf.md).

Creation, SDK and immutable fork recovery tests live in `tests/creation/`.
Its local `path_scopes.toml` retains the creation identity fixture scope.
