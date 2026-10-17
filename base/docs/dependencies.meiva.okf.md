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
[[entry-points.meiva.okf.md|entry points]].

- [[base/lm/docs/lm/lm.meiva.okf.md]] — LLM provider abstraction layer, used by agent/graph/llm/node.py via factory to build chat models
- [[agents-contract.meiva.okf.md]] — agent ↔ gateway state/exception/wire protocol contract
- [[base/agents/messages/docs/kwargs.meiva.okf.md]] — typed `meiva_*` metadata inside a message's `additional_kwargs`
- [[base/agents/messages/docs/inbound-provenance.meiva.okf.md]] — non-enforcing credential, transport, content-hash, and source-assertion facts on gateway inbounds
- [[log.meiva.okf.md]] — structured logging, feeds the unified event emitter (`base/telemetry/emitter.py`)
- [[metrics.meiva.okf.md]] — system-level metrics computation core
- [[agent/db/docs/db.meiva.okf.md]] — base/db/__init__.py provides database connection pool, depended on by services and gateway
- [[gateway-cli.meiva.okf.md]] — gateway communicates with agent processes via the contracts in base/api_contracts/agent_contract.py
- [[base/events/live/docs/live/live.meiva.okf.md]] — `meiva:events` live pub/sub payload union
- [[base/cluster/machine/docs/machine.meiva.okf.md]] — machine name + capability set, `machines` table, spawn-target invariant
- [[base/deploy/schema/docs/migrations.meiva.okf.md]] — baseline + delta schema model, applied set, version assertion
- [[paths.meiva.okf.md]] — `$MEIVA_HOME` layout
- [[install_registry.meiva.okf.md]] — `installed.json` + the skill-scanner gate
- [[base/packages/plugins/docs/enable_config.meiva.okf.md]] — per-machine plugin enable state
- [[host_deploy_state.meiva.okf.md]] — per-host deploy posture
- [[base/agents/impersonation/docs/impersonation.meiva.okf.md]] — cooperative local leases, native consent, external inbox ACKs and handoff
