---
type: doc
title: Agent-runner Side Services — background services run by agent-runner capability
description: "A group of background daemons run on machines whose capabilities include agent-runner — services meaningful only alongside machines that run agents: inbound ops server, crash agent restart, shared headed browser, macOS desktop automation. Source of truth: ServiceSpec.capabilities in ops/spec.py."
tags: []
---

# Agent-runner Side Services

## What is it
Background service group run on machines whose capabilities set includes `agent-runner` (single-machine `gateway,agent-runner` or pure agent-runner). The agent-runner capability owns daemons meaningful only alongside machines that run agents: the ops server dialed by gateway, the restarter that respawns crashed agents, the shared headed browser + MCP upstream, and macOS-specific desktop automation.

`services/agent_runner_side/` is a **capability grouping, not a directory of code** — there is no `services/agent_runner_side/*.py`. Each daemon's code lives in its own `services/<name>/`; which side it runs on is a `ServiceSpec.capabilities` attribute that cuts across the filesystem. See [[gateway_side.ava.okf.md]] for the same note on the mirror group.

## Service List
Source of truth = services in `ops/spec.py` `build_services()` whose `ServiceSpec.capabilities` includes `agent-runner` (`_AGENT_RUNNER` / `_BOTH` groups).

| Service | Responsibility | File |
|------|------|------|
| restarter | agent restart scheduling + orphan reaper | [[restarter.ava.okf.md]] |
| agent-ops | agent-runner inbound HTTP ops (authenticated) | [[agent_ops.ava.okf.md]] |
| browser | headed Chrome reuse + shared MCP upstream | [[browser/browser.ava.okf.md]] |
| permissions-helper | macOS/Windows desktop automation (launchd / logon task, not a session) | [[permissions-helper.ava.okf.md]] |
| computer-mcp | computer-use executor: desktop actions through the signed permissions helper, screen-coordinated (lease + FIFO) + audited (task #1101) | [[computer-mcp.ava.okf.md]] |
| agent-host | hosted runner: every local agent's turns as asyncio tasks in one daemon. Gated off unless `AVA_RUNNER_MODE=hosted` — the default since 2026-09-02 — so it is on every cluster's start roster by default, and `process` is the explicit rollback opt-out | `services/agent_host/` |

## Also Under agent-runner Capability
- **browser-mcp** — shared chrome-devtools-mcp upstream. Gate = browser's PLUS AF_UNIX (its wrapper→daemon transport is a Unix socket), so it is **POSIX-only** where `browser` is not; see [[browser/browser.ava.okf.md]]
- **computer-mcp** — computer-use executor. Capability = permissions-helper + AF_UNIX; no code gate (governance removed 2026-08-10, peer trust model); see [[computer-mcp.ava.okf.md]]
- **agent-runner-watchdog** — watchdog instance for agent-runner capability (`--role agent-runner`; see [[watchdog.ava.okf.md]], one on each side)

## Notes
permissions-helper is macOS/Windows, and **not in the session service roster** — its platform scheduler owns keepalive and it remains outside `build_services()`, while the macOS helper's real protocol healthcheck is manually attached to the agent-runner watchdog.

The hosted runner renews database ownership and then beats local liveness before
publishing its 60-second Redis turn-progress snapshot. That best-effort `SET`
has a three-second operation deadline: a stalled response is cancelled and logged
at WARNING so subsequent ownership renewals continue. The shared Redis client's
long-lived pub/sub reads remain unbounded; shutdown cancellation still propagates.

## Key Dependencies
- [[watchdog.ava.okf.md]] — agent-runner-watchdog keeps alive the session services in this group every 60s (restarter / ops / browser)
- [[services/services.ava.okf.md|Background Services Overview]] — the upper-level index of grouping and capability distribution
