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
An older router rejects these paths; never downgrade an uncertain intent to a
legacy draft route. Automatic ambiguous transport retries remain disabled.
No browser, SDK or MCP consumer activates these new paths in this change.

The semantic input is the parsed draft DTO: `nl` and, for packages, `kind`.
The digest excludes the derived server prompt, label, machine and defaults.
One key with changed semantic input returns 409; a new key represents a new
creation. The existing DTO parsing rules remain intact. The handler renders a
prompt and delegates to the existing birth/launch owner, rather than calling an
LLM itself. Subsequent work in the created conversation may incur model costs.

Only these guarded drafts opt in to `agent_creation_snapshots`. The birth owner
commits the snapshot with the agent, first chat inbound and audit facts, under
the same creation advisory transaction lock. It retains original machine,
config overlay, birth config, launch attempt UUID, first prompt IID, content and
source. There is no target FK, TTL pruning or guessed historical backfill.
A record with a creation key but missing required snapshot fails closed.
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

Legacy draft routes retain their original signatures, response shapes and
one-shot behavior. Legacy HTTP, plain guarded creation, MCP and task assignment
continue using their existing creation namespace and digest. Their shared
`find_creation` default still projects current placement/config/attempt from
`agents_meta`; after explicit retry-launch it can return the replacement attempt.
That separate compatibility gap remains tracked in #4473. It is not silently
fixed or backfilled by this draft-specific snapshot contract.
