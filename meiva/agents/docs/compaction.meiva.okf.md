---
type: doc
title: SDK Guarded Manual Compaction
description: Explicit source observation, keyed acceptance and separate retained compaction status.
tags: [sdk, agents, compaction]
---

# SDK Guarded Manual Compaction

```python
target = meiva.agents.compaction.observe(agent_id)
accepted = meiva.agents.compaction.submit(target, idempotency_key="one-compact-intent")
progress = meiva.agents.compaction.status(accepted)
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
replace the original retry inputs. The SDK never falls back to another compact
route. Transport retries (connection failures and ambiguous outcomes) keep the
exact source and key, and the gateway replays the original acceptance. Separate
intents use separate keys.

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
[[base/agents/compaction/docs/manual-compact/manual-compact.meiva.okf.md]].

## Generated SDK help

```meiva-contract
id = "api-namespace"
kind = "api"
statement = "Explicit manual compaction of one retained, closed source history."
code = ["meiva/agents/compaction.py:<module>"]
tests = ["meiva/tests/namespaces/agents/test_compaction.py::test_compaction_namespace_is_discoverable_in_sdk_help"]
api = "meiva.agents.compaction"
```

```meiva-contract
id = "api-observe"
kind = "api"
statement = "Observe one eligible closed source history for manual compaction.\n\nActive, unsupported or stale sources fail. Keep the returned target with\nthe submission key; a later observation may describe different history."
code = ["meiva/agents/compaction.py:observe"]
tests = ["meiva/tests/namespaces/agents/test_compaction.py::test_fixed_routes_keep_original_source_and_distinguish_status"]
api = "meiva.agents.compaction.observe"
```

```meiva-contract
id = "api-submit"
kind = "api"
statement = "Accept manual compaction of the exact observed source history.\n\nReuse the original target/key after uncertainty. Acceptance does not mean\nthe summary was generated or applied; inspect status separately."
code = ["meiva/agents/compaction.py:submit"]
tests = ["meiva/tests/namespaces/agents/test_compaction.py::test_fixed_routes_keep_original_source_and_distinguish_status"]
api = "meiva.agents.compaction.submit"
```

```meiva-contract
id = "api-status"
kind = "api"
statement = "Inspect execution and application evidence for this original acceptance.\n\nAn accepted command may remain unresolved. Application and release of\nits native continuation are separate facts in the returned status."
code = ["meiva/agents/compaction.py:status"]
tests = ["meiva/tests/namespaces/agents/test_compaction.py::test_fixed_routes_keep_original_source_and_distinguish_status"]
api = "meiva.agents.compaction.status"
```
