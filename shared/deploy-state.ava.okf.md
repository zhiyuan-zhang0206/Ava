---
type: doc
title: Shared — deploy state & liveness (R1)
description: The explicit-model tables + lease APIs — the deployment_state singleton, host_deploy_state, the Gate owner, the host-local pause capability, updater process handoff, the agent alive predicate, and the watcher registry.
tags: []
---

# Shared — deploy state & liveness (R1)

- **Deploy state & liveness (R1)**: the explicit-model tables + lease APIs — the `deployment_state` singleton (cluster deploy lease + phase/kind/settle/last_outcome, [[cluster_lock.ava.okf.md|cluster_lock]]), `host_deploy_state` (per-host posture + updater lease, [[host_deploy_state.ava.okf.md|host_deploy_state]]), the separate generation-guarded Gate owner ([[ui_update_state.ava.okf.md|ui_update_state]]), the host-local exact pause capability ([[pause_owner.ava.okf.md|pause_owner]]) and updater process handoff ([[updater_handoff.ava.okf.md|updater_handoff]]), the agent alive predicate in `shared/db.py` (see [[../agent/agent.ava.okf.md|agent domain]]), and the watcher registry ([[watcher_registry.ava.okf.md|watcher_registry]]).

Parent: [[shared/shared.ava.okf.md|shared libraries]].
