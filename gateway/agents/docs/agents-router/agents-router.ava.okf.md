---
type: doc
title: Agent Router Surfaces
description: Lifecycle, list, state and per-agent observability HTTP surfaces.
tags:
- gateway
- agents
---

# Agent Router Surfaces

## Lifecycle and state

`history/` groups conversation, timeline and context read models with their tests.
Mutation routes remain in `gateway/agents/`.

`/api/agents/*` covers spawn, terminate, resurrect, restart, compact,
send_message, list, and patch — all in the `gateway/agents/` package. CRUD
and spawn live in `router.py`; lifecycle actions live in `lifecycle.py`; message
and state reads live in `state.py`; `forward.py` provides the cross-machine
forwarding helpers. The billing batch-recovery entry is `POST /api/agents/resurrect-billing`
(a read-only preview unless the body sets `execute`; orchestration in
`ops/lifecycle/billing_recovery.py`, per-agent dispatch via the versioned
`resurrect-billing-v1` home action). `ops/rpc_schemas/billing_recovery.py` owns
the distinct `BillingRecoveryHomeResult` and per-agent `BillingRecoveryOutcome` enums,
plus run-level `BillingRecoveryMode` (`dry_run` / `execute`) and
`BillingRecoveryRunOutcome` (`preview` / `executed` / `refused`);
lifecycle dispatch translates home verdicts into batch outcomes. Raw RPC values
are validated by the response models, and JSON wire strings stay unchanged.

`/api/cancel` pauses work using `CancelResult` (`base/agents/contract.py`),
separate from termination. [[control-acceptance.ava.okf.md]] owns keyed cancel
and compact acceptance; acceptance is not native application. The CLI validates
cancel and billing results before reporting success. `/api/models` lists models,
their declared `fast_of` relationships and the effective cluster default using
`base/agents/birth_config.py:resolve_default_model`, also used by the default-model
endpoint, birth stamp and spawn preflight.
`/api/agents/{id}/exited` finalizes agent exit.

## Spawn boundary

[[gateway/agents/docs/agents-router/router-spawn.ava.okf.md]] owns preset resolution,
fork config constraints, creation acceptance and launch retries.

## List projections

`GET /api/agents` is a bounded, newest-first directory page with explicit
scope, label/ID search and a keyset cursor. `GET /api/agents/roster` returns
live cards and their minimal ancestor closure in one database snapshot;
unrelated terminated rows receive no per-agent enrichment. Cards carry
attention counts/priority and an open impersonation session number plus that
lease's phase (`open_impersonation_status`: `requested` / `accepted` /
`active`, or null), never notice bodies — only `active` means the agent is
actually taken over, which the console projects to a distinct `impersonated`
status. Selected or bookmarked agents
use the independent ID detail endpoint. SDK, CLI and MCP consume the same
page contract; no implicit list-all or field-projection compatibility modes
remain.

Cards and detail expose the same typed `availability` projection independently
of lifecycle `status` and `liveness_state`.

`POST /api/agents/{id}/impersonation/force-expire` accepts the session number
the caller observed. It performs a gateway-local DB transition and wake with
standard gateway authentication, returns `expired` or `not_open`, and returns
404 for an unknown agent; no home-runner forwarding.

## Per-agent observability

- `/api/agents/{id}/born-chain` returns the immutable birth chain above an
  agent (nearest ancestor first, each row carrying label / status / machine /
  depth) — one recursive `agents_meta.born_spawner` walk with none of
  `/neighbors`' tie graph. It is the read the inherited-memory context note
  resolves at every window establishment (`plugins/ava_memory/inherit.py`), so
  it is kept deliberately light: no neighbor ranking, no Loki live tail.
- `/api/agents/{id}/token-usage` exposes per-model soft and hard compact
  thresholds for the ContextMeter gauge.
- `/api/agents/{id}/context-breakdown` sums per-message token counts
  (`base/agents/history/message_tokens.py`) of the latest request into kind
  buckets, each with `estimated` / `exact_fraction`; `context_breakdown.py`.

Receipts: [[system-note.ava.okf.md]], [[launch-retry.ava.okf.md]],
[[base/agents/compaction/docs/manual-compact/manual-compact.ava.okf.md|guarded compact]].
