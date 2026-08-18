---
type: doc
title: Chrome MCP — native Chrome + chrome-devtools-mcp upstream
description: Implementation of ava.mcps.chrome — the agent's shared MCP daemon dials browser-mcp's line protocol directly (shared-browser mode) over a Unix socket, so agents share one chrome-devtools-mcp upstream with no per-agent stdio bridge process at all.
tags:
- extensions
- tool
---

# Chrome MCP — native Chrome + chrome-devtools-mcp upstream

## What It Is
`ava.mcps.chrome` lets agents drive a logged-in shared headed Chrome (navigate / click / fill forms / screenshot / DOM / console). It is **not** a standalone MCP server, but rather wraps the upstream `chrome-devtools-mcp` (Chrome DevTools MCP, npm package) into a two-stage bridge of "one upstream, many agents reuse".

## Two and a Half Layers
```
agent's shared MCP daemon (ava._mcps_daemon, in-daemon BrowserLineSession)
      ←Unix socket→ mcp_daemon (per-machine shared, browser-mcp session)
      ←→ chrome-devtools-mcp upstream ←CDP→ shared headed Chrome
```

- **chrome-devtools-mcp upstream** (Chrome DevTools MCP): the npm package that actually speaks CDP to drive Chrome. Its collector subscribes to targets across the **entire browser**, so each upstream buffers network/console traffic for all tabs.
- **mcp_daemon** (`services.browser.mcp_daemon`, ServiceSpec session `browser-mcp`): a per-machine **single** upstream, shared across all agents via a Unix socket. Replaces the old per-agent upstream — N browser agents used to each spawn an upstream, each buffering all browser traffic, making it the #1 memory hog on agent-runner; sharing to 1 means each tab's traffic is received only once.
- **in-daemon line client** (`ava/_mcp_browser.py:connect_browser_direct`, wired via `"shared": "browser"` in `mcps/chrome/.mcp.json`): the agent's shared MCP daemon dials the browser-mcp service's line protocol directly, **no child process at all**. This replaced the per-agent `services.browser.mcp_wrapper` stdio bridge (~63MB RSS per agent). Each agent connection keeps its own socket, so the daemon's per-connection page affinity still isolates agents; tool lists pass through verbatim, so upstream upgrades (new/renamed tools) are automatically reflected. The client self-heals a desynced stream (a response id that does not match the request, or a corrupt line): it rebuilds the socket and restarts the id counter, so a lost response line fails one call but never bricks the connection. The wrapper remains in the tree only as the declared command for hosts running older daemons.

## Two Invariants (daemon side, to make a single upstream safe for multiple clients)
- **Serial lock**: machine-wide one-at-a-time browser operations, multi-step sequences do not interleave (operator chose serial over parallel).
- **Per-connection page affinity**: chrome-devtools-mcp has only one global "current page", but each client has its own tab; the daemon re-selects the connection's page before forwarding page-scoped calls, so A's `click` won't land on B's tab; connections without a page are not forwarded to the global selected page (could be someone else's tab), instead they cold-start (navigate → new_page) or return a no-page error.

## Configuration Layer and Startup Form
chrome is the **builtin (native) layer** in the four-layer MCP config merge (builtin < plugin < installed < machine, see [[ava/mcps.ava.okf.md]]): its definition ships with the code in `mcps/chrome/.mcp.json` and does not enter the install registry (`ava mcp install` installs self-contained packages at the installed layer, e.g., x).

Launch is **relative path direct launch** for the declared command, never `uv run` (the latter is a persistent wrapper process, pure overhead under per-agent spawn): command writes a relative interpreter `.venv/bin/python`, and `ava/_mcp_config.py:server_cwd()` determines the spawn cwd per effective layer — built-in **pins to repo root**, so relative paths resolve to the repo venv. Since `"shared": "browser"` the daemon never spawns this command at all — it dials the browser-mcp service socket directly; the command line is retained for compatibility and as documentation of the underlying bridge.

## Gating / Lifecycle
- Gated together with the browser service (`AVA_BROWSER_ENABLED` + display / Chrome / npx capability) — see [[browser.ava.okf.md|Browser service]].
- **POSIX-only**, on top of that: the wrapper→daemon leg is a Unix socket, so the `.mcp.json` declares `requires: {display, unix_socket}` and the daemon's own gate (`ops/spec.py:_gate_reason` → `browser_mcp_incapability()`) adds the AF_UNIX prong. A Windows agent-runner therefore runs `browser` (a headed Chrome, reachable over CDP) but neither `browser-mcp` nor this MCP entry. Porting the transport: `future/infra/windows-browser-mcp.md`.
- Daemon is kept alive by the agent-runner watchdog every 60s (healthcheck `browser_mcp.py`, Unix socket ping, deliberately not doing upstream roundtrips to avoid killing on slow operations); when upstream dies, daemon auto-reconnects and lets clients retry, rather than exiting.

## Key Dependencies
- [[ava/mcps.ava.okf.md]] — overall MCP call mechanism (`ava.mcps.<server>.<tool>`)
- [[browser.ava.okf.md]] — service/lifecycle for shared Chrome startup + upstream daemon (agent-runner side)

## Entry Points
- `mcps/chrome/.mcp.json` — chrome MCP server definition (`"shared": "browser"` — daemon dials the browser-mcp service directly; declared command = relative direct launch `.venv/bin/python -m services.browser.mcp_wrapper`, retained for older-daemon compatibility)
- `ava/_mcp_browser.py` — in-daemon line-protocol client (the live chrome path)
- `services/browser/mcp_wrapper.py` — the legacy per-agent stdio bridge (kept as declared command; not spawned by current daemons)
- `services/browser/mcp_daemon.py` — per-machine shared upstream daemon (`browser-mcp` session)

## Notes
- Unlike installed MCPs (**installed** layer, `ava mcp install <source>` lands in `$AVA_HOME/mcps/`, each with its own isolated venv), chrome is the **native/built-in** layer, implemented in the browser service and depending on a shared headed Chrome across agents — hence it spans the two subtrees mcps and services/browser.
