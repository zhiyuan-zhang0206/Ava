---
type: doc
title: Sessions
description: Long-running daemon/service processes and agent processes run as named sessions on the platform session backends (native supervisor on POSIX / winproc on Windows; per-session pty hosts for agents' interactive shells). Each session is a record under $AVA_HOME/run/sessions (pty sessions under run/pty) + a log under $AVA_HOME/logs.
tags: []
---

# Sessions

## What it is

Long-running **daemon/service** processes (agent-runner, gateway, services) run as named **sessions on the platform session backend** (`shared/session_backend.get_backend()` — the native process supervisor on POSIX, `shared.winproc` on Windows). An **agent's main process** is a detached native process (double-fork + reparent, `native_proc()` via `ops/agent_launch.py`; see [[process-lifecycle.ava.okf.md]]). The **shell sub-sessions an agent creates** (`ava-agent-<id>-shell-<n>`) each run in their own detached pty host (`get_shell_backend()` → `shared/pty_sessions`), and they are **not** cleaned up when the agent exits — by design, so background work (shell sessions, watchers, Claude Code, etc.) can outlive the process.

Every session has a **record** (`$AVA_HOME/run/sessions/<name>.json`: pid + start time) and a **log** (`$AVA_HOME/logs/<name>.out.log`). `ava cluster status` enumerates the same sessions; raw session output is queried in Loki (the LGTM backend — LogQL via Grafana Explore / logcli / the Loki HTTP API, label `service` = session name; see `deploy/lgtm/README.md`).

## Core Responsibilities

### Session Naming Convention
`shared/cluster.py:session_name(service)` is the single assembly point: `ava-<service>` — the name does **not encode machine or cluster** (each unit's session namespace is its own `$AVA_HOME`, so same-named sessions on different homes never collide). Examples:
- `ava-gateway` — gateway session
- `ava-agent-<id>` — agent main process session (a session record like any other)
- `ava-agent-<id>-shell-<n>[-<name>]` — shell sub-session held by an agent
  - e.g., `ava-agent-1485-shell-2-okf-build` indicates agent 1485's shell #2, named okf-build
- `ava-updater` / `ava-rollout` / `ava-cluster-restart` — orchestration sessions (their logs additionally tee to `$AVA_HOME/logs/{updater,rollout,cluster-restart}-<epoch>.log` on POSIX)

### Environment Variable Forwarding (⚠️ Critical Cross-Cutting Concern)
When an agent process starts, `ava.self.AGENT_ID` is set via command-line argument (`agent/loop.py:main()` establishes identity + `os.environ["AVA_AGENT_ID"]`). Child sessions receive a **built env dict** handed to the backend at spawn (`shared/session_env.forward_env_dict()`) — the caller's HOST-scope env (machine identity, paths, health ports, the gateway URL), while cluster-scope values (db/redis URLs, the cluster secret, provider keys, model knobs) are deliberately NOT forwarded — the child re-sources them at its own boot (fetch on a pure runner, own .env on a gateway host). Detached agent processes get `ops.agent_launch.agent_spawn_env_dict()` (bootstrap keys only). Both passthrough a small set of host environment variables (`$DISPLAY` / `$WAYLAND_DISPLAY`) for capability detection/tools reading OS-native names.

**Delivery is never argv.** The env is an out-of-band child-process environment, so nothing secret ever lands on a command line where `ps -eo command` shows it to any local user (issue #974; the old argv-splice handoff is gone).

Key environment variables in the session chain (all explicitly forwarded via `shared/session_env.py` / `ops.agent_launch.py`, not inherited):
| Variable | Set point | Forwarding method |
|------|--------|---------|
| `AVA_AGENT_ID` | loop.py:main() | **not forwarded** (agent-scope, Task #856) — watcher/schedule children get it inlined into their generated bootstrap (`ava.watcher._build_boot`); a bare shell child has no agent identity |
| `AVA_CLUSTER_SECRET` | cluster-pinned — NOT forwarded: the child reads it from its own .env (gateway host) or presents it from .env to the bootstrap fetch (pure runner) |
| `AVA_HOME` | `install.sh` / converge | `forward_env_dict()` (host-scope) |
| `AVA_MACHINE_HOST` | converge | `forward_env_dict()` (host-scope) |

### Process Exit ≠ Shell Sub-Session Destruction
`agent/lifecycle.py:10-14` explicitly comments (scoped to shell sub-sessions):
> The agent's shell sessions (`ava-agent-<id>-shell-<n>[-<name>]`) are deliberately NOT torn down on exit — they persist across terminate/restart/update so background work outlives the process that started it.

This means:
- Agent terminate → **shell sub-sessions preserved**, shells/Claude Code inside continue running; the main process (and its supervisor session record) is gone
- Agent restart/resurrect → the native supervisor recreates the process + session record; shell sub-sessions untouched, continue surviving
- Orphan session recovery is a **periodic management task**, not a lifecycle hook

## Key Dependencies

- [[lifecycle.ava.okf.md]] — defines session retention policy on exit
- [[env-vars.ava.okf.md]] — full list of environment variables and forwarding chain
- [[process-lifecycle.ava.okf.md]] — on process spawn/restart, the main process + session record are recreated by the native supervisor; shell sub-sessions retained

## Entry Points

- `agent/loop.py:main()` — agent_id set (establish + `AVA_AGENT_ID` env)
- `agent/lifecycle.py` — module docstring states session intentionally not cleaned up on exit
- `ava.shell.sessions` — SDK-exposed session management API

## Notes

- `AVA_AGENT_ID` is deliberately NOT in the session env allowlist (agent-scope, Task #856): watcher/schedule children receive it inlined into their generated bootstrap (`ava.watcher._build_boot`), and a bare persistent-shell child has no agent identity; `agent/loop.py` comment documents this as the single set point
- The session backends host only daemon/service + agent shell sub-sessions — even if all agents exit, the backends keep running
