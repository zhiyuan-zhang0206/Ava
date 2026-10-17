---
type: doc
title: Environment Variables
description: Meiva's **key environment variables** and their propagation chain. These variables define the agent's identity, cluster affiliation, data plane connections, and more.
tags: []
---

# Environment Variables

## What they are

Meiva's **key environment variables** and their propagation chain. These variables define the agent's identity, cluster affiliation, data plane connections, etc.

## Core Responsibilities

### Agent Identity
| Variable | Set at | Purpose |
|------|--------|------|
| `MEIVA_AGENT_ID` | External script bootstrap / disposable execution child; hosted turns use context-bound identity | The agent's numeric ID, globally unique; never forwarded to unrelated daemon/session children |

### Cluster & Data Plane
| Variable | Set at | Purpose |
|------|--------|------|
| `MEIVA_CLUSTER_SECRET` | gateway `meiva init` | The gateway's human bearer (API, frontend login); never served by bootstrap, never held by a remote unit, never a Postgres or Redis password |
| `MEIVA_API_TOKEN` | launch environment | The write generation's machine API token of the process's class (runner class for agents; exec children inherit it); the bearer SDK and service clients present first, and a process launched with the agent or runner profile that lacks one raises instead of presenting `MEIVA_CLUSTER_SECRET`; delivered only while the API is authenticated. Also persisted at 0600 in `$MEIVA_HOME/run/meiva-root/manifests.json` (root's own record of what it launched) until the next start rewrites it; inert once a release fence revokes the generation |
| `MEIVA_DB_URL` | launch environment / bootstrap | The write generation's class login delivered by the launcher (runner class for agents; a remote agent-runner's installed unit capability); `.env` and bootstrap carry only the credential-free endpoint. Same `manifests.json` persistence as `MEIVA_API_TOKEN` above |
| `MEIVA_REDIS_URL` | gateway `.env` / bootstrap | Redis runtime ACL URL; its password remains embedded and is never separately forwarded to agents |
| `MEIVA_HOME` | `meiva start` / converge | Data plane root directory, also **is** the cluster identity itself |

Cluster identity is **path-only** (`base/cluster/`, #629/#633): there is no `MEIVA_CLUSTER` environment variable—the single-machine self-referencing identity is `$MEIVA_HOME` itself; the human-readable label (`home_label()`) is computed from the basename of the home directory for display only, not persisted anywhere; the identity given to a remote agent-runner is the gateway URL + its issued capability bundle, passed to `meiva init --serve-agent-runner --gateway-url ... --machine-name ... --machine-host ...` on its first run. The old `MEIVA_CLUSTER` field is retired: nothing in the current start/identity code reads it, so a legacy gateway payload that still sends it has no effect.

### Network & Host
| Variable | Set at | Purpose |
|------|--------|------|
| `MEIVA_MACHINE_HOST` | converge | Reachable IP/hostname of this machine; Postgres, its pooler and Linux Redis bind to this address (macOS Redis stays loopback-only, relayed off-box by the root-supervised `redis-bridge` unit) |
| `MEIVA_MACHINE_NAME` | converge | Machine name (e.g., my-mac) |
| `MEIVA_GATEWAY_URL` | at startup | Gateway HTTP address |

### SDK Switches
| Variable | Set at | Purpose |
|------|--------|------|
| `MEIVA_SDK_DISABLE` | environment variable | Comma-separated list of SDK module names to disable |
| `MEIVA_LLM_OVERRIDE` | environment variable | Inject custom LLM factory (testing/multi-instance) |
| `MEIVA_SYSTEM_PROMPT_*` | environment variable | Control toggles for system prompt sections |
| `MEIVA_AGENT_COMMUNICATION_STYLE` | environment variable | How verbose the agent is when working: `off` (default) / `oriented` / `concise` / `silent`; `off` omits the section entirely |

### DB
| Variable | Set at | Purpose |
|------|--------|------|
| `MEIVA_DB_NOTIFY_WAIT_TIMEOUT_SECONDS` | `settings.db_notify_wait_timeout_seconds` (`services/agent_runner/agent_host/daemon.py`) | Host subscription read timeout and durable pending-work scan interval |

## Propagation Chain

Environment variables propagate along two paths:
1. **Process fork**: `meiva start` → session backend → agent main process; child processes inherit the launcher's environment.
2. **session / service child**: `meiva.shell.sessions.new()` creates new sessions whose environment comes from the **positive allowlists** in `base/host/env/registry.py` (`child_env(role)` — gateway / runner daemon and session children) — **not** the complete environment of the current process (Task #856 Phase C). Only host-scope facts (machine identity, per-unit health ports, `MEIVA_HOME`, the gateway URL) and the ambient keys below ride; ambient host facts a child cannot re-source are copied non-empty from the launcher's environment — the display sockets / `HOME`, the temp-dir vars, and the machine's network proxy configuration (`HTTP(S)_PROXY` / `ALL_PROXY` / `NO_PROXY`, both spellings — issue #2095: the frontend build's next/font fetch egresses through it, and the values live only in the machine's own environment, never in the repo); cluster-scope values (db/redis URLs, the cluster secret, provider keys) are deliberately not forwarded — the child re-sources them at its own boot (gateway fetch on a runner, its own `.env` on a gateway-capable unit) — and non-modeled identity like `MEIVA_AGENT_ID` never rides into a daemon/session child.

## Key Dependencies

- [[agent/docs/sessions.meiva.okf.md]] — sessions receive env through the forward allowlists (`base/sessions/env_forwarding.py`), not inherited from the launcher
- [[agent/startup/docs/startup.meiva.okf.md]] — At startup, environment variables are read and used for initialization

## Notes

- `MEIVA_AGENT_ID` should be read inside graph nodes via `agent_id_from_config` from `RunnableConfig.configurable.thread_id`, not accessed directly via `meiva.self.AGENT_ID` (noted at the top of `agent/graph/llm/node.py`)
- `MEIVA_SDK_DISABLE` allows per-agent disabling of specific SDK modules (e.g., MCP daemon)
