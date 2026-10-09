---
type: doc
title: Guarded manual compaction consumer audit
description: Actual entry points, fault matrix and pending native owner dependencies.
tags: [agent, compaction, idempotency]
---

# Guarded manual compaction consumer audit

Historical preparation audit for [the protocol design](guarded-manual-compaction.md).
The native protocol is implemented; UI and CLI now use observed closed-history
admission and the old public compact route is removed. Current behavior is owned
by [[base/agents/compaction/docs/manual-compact/manual-compact.ava.okf.md]] and
[[gateway/agents/docs/control-acceptance.ava.okf.md]]. The matrix and proposed gates
below record the preparation state, not current contributor instructions.

## Actual consumers and verification matrix

| Actual owner/consumer | Existing behavior | Slice obligation |
|---|---|---|
| `gateway/agents/lifecycle.py:post_compact` | Optional-key receipt, wake/resurrect | Preserve; new routes separate |
| `ui/web/src/lib/transport/api.ts:compact`, `use-agent-actions.ts` | Mint key per action, legacy HTTP | No activation or retry promise |
| `cli/commands/agents/control.py:cmd_agents_compact` | Legacy HTTP, new UUID, immediate enqueue | Preserve existing CLI |
| `base/db:insert_compact_request_inbound` | Queue/audit producer, tests/admin | No implicit guarded conversion |
| `ava/self.py:compact`, composer `/compact` | Agent-authored summary; composer sends instruction | Preserve distinct semantics |
| IM core/GatewayClient | Unknown slash goes through ordinary chat; no compact HTTP method | Do not invent a new consumer |
| MCP endpoint/fleet SDK | No registered manual compact operation | No claimed activation |
| claim routing/dispatch/decide | Done-before-LLM; multi-request last summary; chat deferral | New pointer dispatch isolated |
| compact hook/transition/init_context | Shared prompt and history replacement | Reuse; auto/self unchanged |
| host invocation/continuation/startup | Flush, native settlement, inbound reconcile | Recover exact command before ordinary work |
| checkpoint history/chunks/timeline | Boundary and `ava_compact_id` readers | Frozen anchor, stable original result metadata |

Real PostgreSQL + actual graph/host tests must cover fresh concurrent same-key,
changed target, distinct-key same source, NOOP then new input, ended-work eligibility
and idling-with-ACTIVE refusal. Inject before/after generation claim, provider
response/result commit, transition, init_context, flush and application ACK.
Use buffered saver intervals and fresh readers: prepared result survives restart;
unretained result never automatically recalls provider; marker settles lost ACK.
Cover concurrent cancel/chat/self-auto compact, certified and uncertified transfer,
multiple successors, original owner late completion, source deletion/terminal
replay, one-connection pool across provider work, old router/invalid protocol/auth,
and ordinary startup after terminal uncertainty. Assert actual call counts,
summary/history identity, original IID disposition and next work UUID.

## Dependency and implementation gate

Reuse public `load_work`, `receiver`, typed `NativeWorkTransfer` and
`ResourceTransferProof`, resource admission/closure facts, host serialized
continuation, history latest-head and cold saver owners after their final pin.
Do not generalize `require_cancel_receiver` or copy cancel-specific models.
Ended-work compact eligibility needs an explicit new domain projection under
these facts: current no-command native work settlement alone does not establish
all compact quiescence requirements. Resolve the exact checkpoint channel-version
type and closed-resource proof at that pinned owner before DDL/code generation.
The new command must hook into claim and cold startup before ordinary work;
route advertisement/ready PR waits for the whole proof chain and negative tests.

### Proposed storage and response boundaries

DDL is not applied in this preparation slice. Final naming follows the pinned
native work owner; use additive tables and synchronized schema/role grants.

| Record | Retained fields and constraints |
|---|---|
| Compact source observation | Observation UUID; agent ID; producer protocol; actual receiver attribution; original ended work; typed resource-closure proof; source checkpoint/namespace; messages and compact channel versions; segment version. Required fields cannot be defaulted from missing evidence. |
| Compact command | Command UUID; unique scoped operation key; immutable raw request and acceptance; observation snapshot; original attempt UUID when generation claimed; outcome/reason; prepared result snapshot/hash; applied checkpoint ID when applied. No retained bearer or queue-cleanup FK. |
| Agent pending pointer | One exact pending compact command serialized with native owner/command arbitration. Terminal release must CAS that original pointer; no blind clear of a successor's command. |

Table CHECKs tie prepared result to its claimed attempt, APPLIED to a complete
result/marker/checkpoint proof, and NOOP to no generation/application. Unknown
states and malformed typed snapshots fail fast. Acceptance is the original
`command_id + target` pair; status exposes current outcome and bounded reason,
not readiness. Prepared result retains summary and metadata needed by the existing
message/transition owner; decode through those types, not a second public message
schema. Failed/lost hint delivery cannot replace the immutable business outcome.

Observed channel versions come from the checkpoint owner's `channel_versions`
mapping. Existing history SQL compares its messages version through `->>`;
implementation must normalize with that owner rather than inventing a numeric
monotonic order or treating checkpoint UUIDs as a history watermark.

### Nested provider retries (actual locked environment)

`generate_summary` calls `agent.llm.cache.ainvoke_with_cache_retry`: it wraps the
whole exchange in `llm_compact_timeout_seconds` and retries once on a specifically
classified stale explicit-cache error; it does not broadly retry other failures.
The legacy manual handler separately retries unknown exceptions. Do not reuse
that loop or emergency no-LLM fallback for the guarded request.

OpenAI/Anthropic provider constructors do not explicitly disable SDK retries.
Locked model inspection found ChatOpenAI `max_retries=None` delegating to OpenAI's
default 2, ChatAnthropic default 2 and ChatGoogleGenerativeAI default 6. Thus even
one helper invocation is not proof of one vendor request. Before implementation,
select a provider-owned single-attempt construction/invocation policy or reject
providers that cannot express it, without modifying legacy models. Reuse the
prompt/preparation owner; do not mutate a shared LLM instance. Classified explicit
cache rejection must remain distinct from ambiguous timeout/response loss.

Audit evidence: `where_used` for `post_compact` and `generate_summary`, plus actual
route/UI/CLI/IM/MCP searches. Existing endpoint + hosted failure tests passed
9 tests against the native-cancel WIP during read-only review. This proves the
legacy contract only; none of the proposed guarded fault tests exists yet.
