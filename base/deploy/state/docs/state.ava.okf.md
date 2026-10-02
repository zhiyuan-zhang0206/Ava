---
type: doc
title: Base — deploy state & liveness (R1)
description: The explicit-model deploy state — the host_deploy_state posture row, the home lifecycle mutex, the host-local pause capability, and the agent alive predicate.
tags: []
---

# Base — deploy state & liveness (R1)

- **Deploy state & liveness (R1)**: the explicit-model state — `host_deploy_state` (per-host posture, [[host_deploy_state.ava.okf.md|host_deploy_state]]), the home lifecycle mutex ([[home_lifecycle_locks.ava.okf.md|home_lifecycle_locks]]) and the host-local exact pause capability ([[pause_owner.ava.okf.md|pause_owner]]), and the agent alive predicate in `base/db/__init__.py` (see [[agent/docs/agent.ava.okf.md|agent domain]]). The `deployment_state` singleton's live consumer is the [[base/db/docs/code-version-gate.ava.okf.md|code-version gate]]; no cluster deploy lease is taken on it.

Parent: [[base/docs/base.ava.okf.md|base library]].
