---
type: doc
title: Browser — Headed Chrome Reuse Service
description: Shared headed Chrome management service on agent-runner — a three-component architecture that enables one Chrome instance to be reused by all agents. Replaces the previous per
tags: []
---

# Browser — Headed Chrome Reuse Service

## What is it
A shared headed Chrome management service on agent-runner — a three-component architecture that enables one Chrome instance to be reused by all agents, replacing the previous N-fold memory waste of each agent spawning its own Chrome.

**Role affiliation**: agent-runner side (gateway does not run) — `ServiceSpec.capabilities=_AGENT_RUNNER` in `ops/spec.py`; the roster is derived by `services_for_capabilities` intersecting with the local `machine_role()`.

## Core Responsibilities
- **Shared Chrome instance**: `daemon.py` launches a headed Chrome with a CDP remote-debugging port and a dedicated persistent profile. The supervised session must OWN that Chrome, which takes two shapes: `os.execvp` on POSIX (the pane's process becomes Chrome), and — because Windows has no exec, so `os.execvp` there spawns-and-exits and the tracked pid dies while Chrome lives — staying Chrome's parent (`_launch` → `_supervise_chrome`)
- **Browser liveness is CDP, not a child pid**: Chrome's launched process is not always the process that stays alive — on a `SingletonLock` handoff the binary forwards its command line to another browser process and exits with success while Chrome runs on. The Windows supervisor therefore treats a child exit as a *question*, answered by `_cdp_confirmed_gone` (a bounded CDP probe: ≤6 attempts, ≤10s wall clock). CDP unreachable → the browser is gone, exit non-zero for a respawn; CDP reachable → handoff, so keep the session alive and watch CDP (`_watch_cdp`), exiting only once the endpoint goes away. A deliberate Ctrl-Break stop skips the probe. POSIX needs none of this: the exec'd pane process only ends when Chrome does
- **Shared MCP upstream**: `mcp_daemon.py` runs a `chrome-devtools-mcp` upstream, subscribing to all browser targets, shared by all agents (SDK v2 `ClientSession` — `send_ping()` liveness probe, the v2 rename of `ping()`; float read timeout)
- **Two invariants**: serial lock (machine-wide, one browser operation at a time, no interleaving of multi-step sequences) + per-connection page affinity (each connection remembers its own current page; page-scoped calls first re-select before forwarding, so A's click won't land on B's tab)
- **Automatic upstream reconnection** (#457): when upstream dies, the daemon automatically reconnects and lets clients retry, rather than exiting
- **Per-agent MCP bridge**: `mcp_wrapper.py` provides each agent with a stdio MCP server, forwarding to the shared upstream via Unix socket (SDK v2 lowlevel `Server` — constructor-handler API `on_list_tools` / `on_call_tool`; raw arguments pass through unvalidated, the upstream does its own validation)
- **Memory optimization**: reduced from N upstreams (each buffering all tab network/console traffic) to 1
- **Strongly-typed line protocol**: `protocol.py` defines `Request`/`Response` (`OkResponse | ErrResponse`, discriminated by `ok`) TypedDict — three processes, daemon (server), wrapper (client), `healthchecks/browser_mcp.py` (probe), share the same wire types, no longer hand-writing dict shapes individually.

## Gating
- **browser** — `AVA_BROWSER_ENABLED` + `browser_incapability()` (display + Chrome + npx). Runs on Windows.
- **browser-mcp** — the same prongs PLUS AF_UNIX (`browser_mcp_incapability()`), because the wrapper→daemon leg is a Unix socket. **POSIX-only**: a Windows agent-runner gets a headed Chrome over CDP and no MCP front end, and the `chrome` MCP entry is gated off with it (`requires: {display, unix_socket}`). Porting the transport: `future/infra/windows-browser-mcp.md`.

## Key Dependencies
- [[ava/mcps.ava.okf.md]] — `chrome-devtools-mcp` is the upstream
- [[services/watchdog/watchdog.ava.okf.md]] — keeps alive via `healthchecks/browser.py` (identity-verified CDP **and** ava-browser session liveness — CDP alone can tell neither a supervised Chrome from an orphan holding the port, nor ours from another unit's) and `healthchecks/browser_mcp.py` (Unix socket ping)

## Entry Points
- `services/browser/daemon.py` — Chrome launch (`AVA_BROWSER_ENABLED` **defaults to True**; `browser_incapability()` auto-gates machines lacking display/Chrome/npx)
- `services/browser/mcp_daemon.py` — shared MCP upstream
- `services/browser/mcp_wrapper.py` — per-agent MCP bridge
- `services/browser/protocol.py` — line-protocol types shared by three processes
- `services/browser/orphan.py` — identify + reap a Chrome on this cluster's profile that left the session tree (called from `_do_stop(keep_browser=False)`)
- `services/browser/probe.py` — is the Chrome on this cluster's CDP port OURS (`DaemonProbe` verdict); the roster's `ServiceSpec.identity_probe` and `healthchecks/browser.py` both run it

## Notes
- **The profile is also how a probe knows the browser is ours**: CDP exposes no field we control — measured, `/json/version` carries only browser/protocol/UA/V8/WebKit strings plus a per-launch websocket uuid, and `DevToolsActivePort` is written only when the port is auto-assigned — so `probe.py` reuses `orphan.py`'s `--user-data-dir` token and adds the half a profile match alone cannot give: the identified Chrome must hold the LISTEN socket on the CDP port. Existing is not owning; on a box with two localhost namespaces (WSL2) our Chrome can be alive with a dead DevTools endpoint while a relayed one answers. The probe asks the question in BOTH directions — the walk direction above, plus a listener-first direction that reads the global TCP table for the pid owning the LISTEN socket and asks whether THAT pid is ours (by the walk, or by its own argv). Either may win; the listener-first arm exists because the walk direction alone has blind spots (an unreadable argv, a failed socket read) that would misread our own listener as a foreign occupant.
- **An unsupervised Chrome of ours heals itself**: when the probe reads ALIVE but the `ava-browser` session is gone, the healthcheck no longer just names the operator remedy (`ava stop --stop-browser` + `ava start`) every 60s — it performs it: sweep the orphan via `orphan.reap_cluster_chrome` (identity-verified by profile) and respawn the session in the same round. The machine-1 1,094-ERRORs/day shape closes within one round instead of waiting for a human.
- **The healthcheck's ERROR lines are episode-gated**: one line per failure episode + a 6h reminder; quiet rounds log DEBUG ([[services/healthchecks/terminal-verdict/terminal-verdict.ava.okf.md]]).
- **A teardown reaches Chrome by profile, not by process tree**: killing the `ava-browser` session cannot reach a Chrome that left the tree on a `SingletonLock` handoff, so `ava stop --stop-browser` / `ava cluster destroy` additionally name Chrome by this cluster's own `--user-data-dir` and kill it. Identification, the argument it cannot select the operator's browser, scope and ordering: [[services/agent_runner_side/browser/browser-teardown.ava.okf.md]]
- `AVA_BROWSER_ENABLED` **defaults to True** (not opt-in) — auto-detects host capability (display + Chrome + npx); if unavailable, `browser_incapability()` automatically skips (`shared/platform_probes.py:123`; applied as a service gate in `ops/spec.py`)
- Chrome profile is persistent, retaining login state
- **no data plane**: neither daemon opens a Postgres connection at boot or at runtime (their whole data plane is CDP + the Unix socket), so both specs declare `requires_db=False` and the watchdog keeps reviving them through a DB outage or a schema mismatch — a DB-scoped round block holds back only the DB's users ([[services/watchdog/watchdog.ava.okf.md]])
- mcp_wrapper transparently passes the tool list; upstream version upgrades automatically reflect
