---
type: doc
title: Shared — deploy state & liveness (R1)
description: The explicit-model tables + lease APIs — the deployment_state singleton, host_deploy_state, the Gate owner, the host-local pause capability, updater process handoff, and the agent alive predicate.
tags: []
---

# Shared — deploy state & liveness (R1)

- **Deploy state & liveness (R1)**: the explicit-model tables + lease APIs — the `deployment_state` singleton (cluster deploy lease + phase/kind/settle, [[cluster_lock.ava.okf.md|cluster_lock]]), `host_deploy_state` (per-host posture + updater lease, [[host_deploy_state.ava.okf.md|host_deploy_state]]), the home lifecycle mutexes ([[home_lifecycle_locks.ava.okf.md|home_lifecycle_locks]]), the host-local exact pause capability ([[pause_owner.ava.okf.md|pause_owner]]) and updater process handoff ([[base/deploy/updater/docs/handoff.ava.okf.md|updater handoff]]), and the agent alive predicate in `base/db/__init__.py` (see [[agent/docs/agent.ava.okf.md|agent domain]]).

Parent: [[base/docs/base.ava.okf.md|base library]].
