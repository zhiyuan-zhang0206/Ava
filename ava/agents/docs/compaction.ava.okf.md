---
type: doc
title: SDK Guarded Manual Compaction
description: Explicit source observation, keyed acceptance and separate retained compaction status.
tags: [sdk, agents, compaction]
---

# SDK Guarded Manual Compaction

```python
target = ava.agents.compaction.observe(agent_id)
accepted = ava.agents.compaction.submit(target, idempotency_key="one-compact-intent")
progress = ava.agents.compaction.status(accepted)
```

`observe` reads the fixed `compact-target` route and validates the canonical
`CompactTarget`, including the matching agent. Only an eligible new native
host's actual closed source history can qualify. Active, unsupported or stale
sources are refused by the domain owner. Observation is not cached capability
negotiation or a status-based substitute for source evidence.

`submit` revalidates the original target before HTTP and requires a caller key
with explicit `principal-v1` scope on the fixed `compact-history` route. The
202 canonical receipt must match that source. Retain this target/key after
uncertainty; another observation can select different history and must not
replace the original retry inputs. The SDK never falls back to legacy compact
or retries an ambiguous response automatically. Connect-family transport
retries keep the exact source and key. Separate intents use separate keys.

`status` targets the receipt's original command and validates the returned
`CompactStatus`, including its matching immutable acceptance. The canonical
outcome and execution evidence remain separate from `continuation_released`.
Acceptance is not provider completion, application or native resource closure;
even an applied result may still await release of its original continuation.
No polling, provider/model invocation, new worker or client journal is added.

Each call rechecks attached external identity before HTTP. Authentication and
native source eligibility remain server-owned; this adds no server ACL or
lease renewal. Legacy automatic compaction, SDK self-compaction, UI/CLI/MCP
consumers and runtime rollout remain separate. Operator compatibility and
uncertainty/retention boundaries remain with the existing domain owner:
[[base/agents/compaction/docs/manual-compact/manual-compact.ava.okf.md]].
