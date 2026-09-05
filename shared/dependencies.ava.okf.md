---
type: doc
title: Shared library dependencies
description: Dependency map for shared contracts, state, infrastructure, and agent identity.
tags:
- shared
- library
---

# Shared library dependencies

The shared-layer domain map below complements the public
[[entry-points.ava.okf.md|entry points]].

- [[shared/lm/lm.ava.okf.md]] — LLM provider abstraction layer, used by agent/graph/_llm.py via factory to build chat models
- [[agents-contract.ava.okf.md]] — agent ↔ gateway state/exception/wire protocol contract
- [[shared/message_kwargs.ava.okf.md]] — typed `ava_*` metadata inside a message's `additional_kwargs`
- [[inbound-provenance.ava.okf.md]] — non-enforcing credential, transport, content-hash, and source-assertion facts on gateway inbounds
- [[log.ava.okf.md]] — structured logging, feeds the unified event emitter (`shared/telemetry.py`)
- [[metrics.ava.okf.md]] — system-level metrics computation core
- [[db.ava.okf.md]] — shared/db.py provides database connection pool, depended on by services and gateway
- [[gateway-cli.ava.okf.md]] — gateway communicates with agent processes via the contracts in shared/agents.py
- [[shared/live_events.ava.okf.md]] — `ava:events` live pub/sub payload union
- [[shared/machine.ava.okf.md]] — machine name + capability set, `machines` table, spawn-target invariant
- [[shared/migrations.ava.okf.md]] — baseline + delta schema model, applied set, version assertion
- [[paths.ava.okf.md]] — `$AVA_HOME` layout
- [[install_registry.ava.okf.md]] — `installed.json` + the skill-scanner gate
- [[shared/plugins_config.ava.okf.md]] — per-machine plugin enable state
- [[cluster_lock.ava.okf.md]] — the cluster deploy lease
- [[host_deploy_state.ava.okf.md]] — per-host deploy posture + updater lease
- [[watcher_registry.ava.okf.md]] — the `agent_watchers` registry
- [[impersonation.ava.okf.md]] — cooperative local leases, native consent, external inbox ACKs and handoff
