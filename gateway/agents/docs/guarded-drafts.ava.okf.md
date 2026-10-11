---
type: doc
title: Guarded Draft Agent Creation
description: Raw draft intent and immutable first birth receipts for guide, schedule and package agents.
---

# Guarded Draft Agent Creation

`POST /api/keyed/v1/guide/draft`, `/api/keyed/v1/schedules/draft` and
`/api/keyed/v1/packages/draft` require a valid caller `Idempotency-Key`, exact
`Idempotency-Scope: principal-v1` and a verified principal on every request,
including replay. Their identities include the principal, POST and exact
versioned path. Missing or invalid admission has no birth or launch effects.
The browser makes one fetch per submission and never retries automatically.
The browser's Guide, Schedule Maker and Package Installer entries use these
paths. Each submission gets one operation key; transport helpers also accept
an explicit caller key for same-intent replay. They require a 200 receipt with
a positive agent ID and make one fetch.
The entry mutations explicitly disable automatic retries and ignore Enter
while a submission is pending. SDK and MCP draft consumers remain unchanged.

The semantic input is the parsed draft DTO: `nl` and, for packages, `kind`.
The digest excludes the derived server prompt, label, machine and defaults.
One key with changed semantic input returns 409; a new key represents a new
creation. The existing DTO parsing rules remain intact. The handler renders a
prompt and delegates to the existing birth/launch owner, rather than calling an
LLM itself. Subsequent work in the created conversation may incur model costs.

Every keyed creation (guarded drafts, plain creation, the MCP tool and task
assignment) records an `agent_creation_snapshots` row.
The birth owner commits the snapshot with the agent, first chat inbound and audit facts, under
the same creation advisory transaction lock. It retains original machine,
config overlay, birth config, launch attempt UUID, first prompt IID, content and
source. There is no target FK, TTL pruning or historical backfill.
Rollback leaves no birth, prompt or snapshot. Concurrent retries accept the
winning birth without inserting another prompt.

Replay reads the snapshot before mutable spawn checks or default resolution.
Only the original attempt, still idling, unadmitted and on its original machine,
may receive the existing repeatable launch wake; the runner also validates the
attempt fence. Admission, termination, changed placement, deleted metadata or a
later explicit retry-launch attempt causes a historical acceptance response
with the original agent ID and no launch. Replays never revert operator config,
rotate attempts or insert prompts. A response is historical acceptance, not
proof that the conversation currently exists, is available or executed.

Plain guarded creation, the MCP tool and task assignment retain their original
birth through the same snapshot owner; each keeps its own request digest and
operation namespace.
