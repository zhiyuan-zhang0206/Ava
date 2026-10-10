---
type: doc
title: Base library dependencies
description: Dependency map for shared contracts, state, infrastructure, and agent identity.
tags:
- base
- library
---

# Base library dependencies

The base-layer domain map below complements the public
[[entry-points.ava.okf.md|entry points]].

- [[base/lm/docs/lm/lm.ava.okf.md]] — LLM provider abstraction layer, used by agent/graph/llm/node.py via factory to build chat models
- [[agents-contract.ava.okf.md]] — agent ↔ gateway state/exception/wire protocol contract
- [[base/agents/messages/docs/kwargs.ava.okf.md]] — typed `ava_*` metadata inside a message's `additional_kwargs`
- [[base/agents/messages/docs/inbound-provenance.ava.okf.md]] — non-enforcing credential, transport, content-hash, and source-assertion facts on gateway inbounds
- [[log.ava.okf.md]] — structured logging, feeds the unified event emitter (`base/telemetry/emitter.py`)
- [[metrics.ava.okf.md]] — system-level metrics computation core
- [[agent/db/docs/db.ava.okf.md]] — base/db/__init__.py provides database connection pool, depended on by services and gateway
- [[gateway-cli.ava.okf.md]] — gateway communicates with agent processes via the contracts in base/agents/contract.py
- [[base/events/live/docs/live/live.ava.okf.md]] — `ava:events` live pub/sub payload union
- [[base/cluster/docs/machine.ava.okf.md]] — machine name + capability set, `machines` table, spawn-target invariant
- [[base/deploy/schema/docs/migrations.ava.okf.md]] — baseline + delta schema model, applied set, version assertion
- [[paths.ava.okf.md]] — `$AVA_HOME` layout
- [[install_registry.ava.okf.md]] — `installed.json` + the skill-scanner gate
- [[base/packages/plugins/docs/enable_config.ava.okf.md]] — per-machine plugin enable state
- [[host_deploy_state.ava.okf.md]] — per-host deploy posture
- [[base/agents/impersonation/docs/impersonation.ava.okf.md]] — cooperative local leases, native consent, external inbox ACKs and handoff
