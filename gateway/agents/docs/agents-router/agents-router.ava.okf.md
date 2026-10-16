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

`/api/agents/*` covers spawn, terminate, resurrect, restart,
send_message, list, and patch — all in the `gateway/agents/` package. CRUD
and spawn live in `router.py`; lifecycle actions live in `lifecycle.py`; message
and state reads live in `state.py`; `forward.py` provides the cross-machine
forwarding helpers. New message delivery retains an exact-input home-machine wake and bounded sender retry. Billing batch recovery is removed.

Versioned `/api/keyed/v1/agents/{id}/cancel-work` and `compact-history` accept
observed native work and closed history targets with required principal-scoped
keys. [[control-acceptance.ava.okf.md]] owns their consumer contract; acceptance
is not native application. The former `/api/cancel` and unversioned compact
routes are removed. The CLI validates these results and billing outcomes before
reporting acceptance. `/api/models` lists models, their declared `fast_of`
relationships and the effective cluster default using
`base/agents/model/birth_config.py:resolve_default_model`, also used by the default-model
endpoint, birth stamp and spawn preflight.
`/api/agents/{id}/exited` finalizes agent exit.

The model picker receives `superseded_by` and each model's reasoning-effort
options from the registry. `gateway/tests/test_agents_endpoints.py` compares
those options with the corresponding provider binding: shared vocabularies
match its declared levels, while Gemini's per-model vocabularies stay within the
binding's fallback. The published effort default is the model's tuning value,
nonempty and within its options. A cluster-wide effort pin does not replace
that picker default.

Lifecycle handlers pass the actual request app's database and pool to
`forward_to_home_machine`; home lookup borrows that pool and RPC uses that
database. MCP termination passes its server-composed database and pool through
`terminate_agent_with_open_tasks`, including the advisory task hint and shell
TTL cleanup. Forward helpers do not import the assembled `gateway.app`.

## Spawn boundary

[[gateway/agents/docs/agents-router/router-spawn.ava.okf.md]] owns fork config
constraints, creation acceptance and launch retries.

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
  buckets, each with `estimated` / `exact_fraction`; `base/agents/history/context_breakdown.py`.

Receipts: [[system-note.ava.okf.md]], [[gateway/agents/docs/launch-retry.ava.okf.md]],
[[base/agents/compaction/docs/manual-compact/manual-compact.ava.okf.md|guarded compact]].


## Recovery and message boundaries

A failed catalog does not invalidate an already committed chat receipt. Replay
compares the normalized original content before current image/model checks;
missing receipts still validate uploaded images before inserting anything.
Plain text delivery does not need model metadata. Image references must belong
to the addressed agent's existing upload; arbitrary filesystem paths and remote
URLs are not admitted as uploads. An image-only inbound retains a text placeholder
for envelope/timeline consumers alongside its native content blocks. The 1 MiB
message limit bounds pending/checkpoint storage and telemetry volume.

Termination's open-task hint and shell-TTL cleanup are advisory after acceptance.
Failed hint/cleanup work is logged; it cannot undo or block accepted termination.
The TTL reaper eventually retires leftover shell rows. Label edits and their audit
fact commit together; agent-attributed edits also reach the active impersonation
lease log through the shared audit owner.

Context-budget views require a catalog even when checkpoint counts are absent.
An unavailable catalog is an explicit 503 rather than a fabricated zero ceiling.
The existing unknown-model-window display handling and checkpoint-read tolerance
remain separate from catalog availability.

## Model capability availability

Directory, roster and ID-addressed detail remain readable after a retained model
catalog startup failure. Their required `supports_vision` field is null until a
healthy gateway generation can resolve it; other database facts and enum checks
remain unchanged. Projection ownership is
[[base/agents/observation/docs/observation.ava.okf.md]]. Creation and image messages
still require a complete catalog. Ordinary text delivery records an inbound
receipt, and an exact-work stop records cancellation acceptance: neither receipt
proves that the runner executed the message or ended the work.
