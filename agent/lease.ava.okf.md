---
type: doc
title: Agent Runtime Ownership
description: Hosted incarnation leases fence admission, settlement and resource cleanup.
tags: []
---

# Agent Runtime Ownership

`agents_meta.runtime_generation`, `runtime_owner`, `runtime_kind` and
`lease_expires_at` identify the admitted hosted runtime. The generation scopes an
agent incarnation; the owner identifies the host boot. A delayed task cannot
renew or settle another incarnation.

Ownership spans normal turns, idle and model-cache eviction. Idle has no active
task even when its host retains ownership. `renew_hosted_owner()` refreshes only
the current host owner's rows; `release_hosted_owner()` and
`settle_hosted_runtime()` match the original incarnation. Restart releases it for
a new generation. A fresh foreign owner blocks admission and stale-row recovery.

Lease TTL and renewal ordering live in `shared/deploy_timing.py` and
`shared/timing.py`. A lease is runtime ownership evidence, not an agent identity
count: the status page counts non-terminated local identities, including idle
and maintenance-paused agents. Heartbeat selects eligible idle identities
without requiring an independently resident agent process.

## Entry points

- `agent/hosted_ownership.py` — admission, renewal, settlement and release
- `services/agent_host/host.py:settle_stale_running_rows` — host recovery
- `services/agent_host/daemon.py` — owner health beat
- `shared/runtime_incarnation.py` — context-bound execution identity

Related: [[startup/admission.ava.okf.md]] and [[lifecycle.ava.okf.md]].
