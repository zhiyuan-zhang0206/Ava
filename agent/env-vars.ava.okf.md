---
type: doc
title: Environment Variables
description: Ava's **key environment variables** and their propagation chain. These variables define the agent's identity, cluster affiliation, data plane connections, and more.
tags: []
---

# Environment Variables

## What they are

Ava's **key environment variables** and their propagation chain. These variables define the agent's identity, cluster affiliation, data plane connections, etc.

## Core Responsibilities

### Agent Identity
| Variable | Set at | Purpose |
|------|--------|------|
| `AVA_AGENT_ID` | External script bootstrap / disposable execution child; hosted turns use context-bound identity | The agent's numeric ID, globally unique; never forwarded to unrelated daemon/session children |

### Cluster & Data Plane
| Variable | Set at | Purpose |
|------|--------|------|
| `AVA_CLUSTER_SECRET` | `ava start` or enroll | Control-plane bearer for the gateway API, `/ops`, bootstrap, and machine registration; never a Postgres or Redis password |
| `AVA_DB_URL` | gateway `.env` / bootstrap | Gateway owner URL locally; `ava_runner` URL when projected to an agent-runner |
| `AVA_REDIS_URL` | gateway `.env` / bootstrap | Redis runtime ACL URL; its password remains embedded and is never separately forwarded to agents |
| `AVA_HOME` | `install.sh` / converge | Data plane root directory, also **is** the cluster identity itself |

Cluster identity is **path-only** (`shared/cluster/`, #629/#633): there is no `AVA_CLUSTER` environment variable—the single-machine self-referencing identity is `$AVA_HOME` itself; the human-readable label (`home_label()`) is computed from the basename of the home directory for display only, not persisted anywhere; the identity given to a remote agent-runner during enrollment is the gateway URL + cluster secret. The old `AVA_CLUSTER` field is retired, and `cli/enroll.py` simply ignores that key if a legacy gateway payload still sends it.

### Network & Host
| Variable | Set at | Purpose |
|------|--------|------|
| `AVA_MACHINE_HOST` | converge | Reachable IP/hostname of this machine; Postgres and its pooler bind to this address (Redis remains loopback-only) |
| `AVA_MACHINE_NAME` | converge | Machine name (e.g., my-mac) |
| `AVA_GATEWAY_URL` | at startup | Gateway HTTP address |

### SDK Switches
| Variable | Set at | Purpose |
|------|--------|------|
| `AVA_SDK_DISABLE` | environment variable | Comma-separated list of SDK module names to disable |
| `AVA_LLM_OVERRIDE` | environment variable | Inject custom LLM factory (testing/multi-instance) |
| `AVA_SYSTEM_PROMPT_*` | environment variable | Control toggles for system prompt sections |
| `AVA_AGENT_COMMUNICATION_STYLE` | environment variable | How verbose the agent is when working: `off` (default) / `oriented` / `concise` / `silent`; `off` omits the section entirely |

### DB
| Variable | Set at | Purpose |
|------|--------|------|
| `AVA_DB_NOTIFY_WAIT_TIMEOUT_SECONDS` | `settings.db_notify_wait_timeout_seconds` (`services/agent_host/daemon.py`) | Host subscription read timeout and durable pending-work scan interval |

## Propagation Chain

Environment variables propagate along two paths:
1. **Process fork**: `ava start` → session backend → agent main process; child processes inherit the launcher's environment.
2. **session / service child**: `ava.shell.sessions.new()` creates new sessions whose environment comes from the **positive allowlists** in `shared/env_registry.py` (`child_env(role, platform)` — daemon/session children vs agent children) — **not** the complete environment of the current process (Task #856 Phase C). Only host-scope facts (machine identity, per-unit health ports, `AVA_HOME`, the gateway URL) and explicit guide keys ride; cluster-scope values (db/redis URLs, the cluster secret, provider keys) are deliberately not forwarded — the child re-sources them at its own boot (gateway fetch on a runner, its own `.env` on a gateway-capable unit) — and non-modeled identity like `AVA_AGENT_ID` never rides into a daemon/session child.

## Key Dependencies

- [[sessions.ava.okf.md]] — sessions receive env through the forward allowlists (`shared/session_env.py`), not inherited from the launcher
- [[startup/startup.ava.okf.md]] — At startup, environment variables are read and used for initialization

## Notes

- `AVA_AGENT_ID` should be read inside graph nodes via `agent_id_from_config` from `RunnableConfig.configurable.thread_id`, not accessed directly via `ava.self.AGENT_ID` (noted at the top of `agent/graph/_llm.py`)
- `AVA_SDK_DISABLE` allows per-agent disabling of specific SDK modules (e.g., MCP daemon)
