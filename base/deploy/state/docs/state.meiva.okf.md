---
type: doc
title: Base — deploy state & liveness (R1)
description: The explicit-model deploy state — the host_deploy_state posture row, the home lifecycle mutex, the host-local pause capability, and the agent alive predicate.
tags: []
---

# Base — deploy state & liveness (R1)

- **Deploy state & liveness (R1)**: the explicit-model state — `host_deploy_state` (per-host posture, [[host_deploy_state.meiva.okf.md|host_deploy_state]]), the home lifecycle mutex ([[home_lifecycle_locks.meiva.okf.md|home_lifecycle_locks]]) and the host-local exact pause capability ([[pause_owner.meiva.okf.md|pause_owner]]), and the agent alive predicate in `base/agents/inbox/live.py` (see [[agent/docs/agent.meiva.okf.md|agent domain]]). The `deployment_state` singleton's live consumer is the [[base/db/docs/code-version-gate.meiva.okf.md|code-version gate]]; no cluster deploy lease is taken on it.

Parent: [[base/docs/base.meiva.okf.md|base library]].
