---
type: doc
title: Browser — Headed Chrome Reuse Service
description: Shared headed Chrome management service on agent-runner — a three-component architecture that enables one Chrome instance to be reused by all agents. Replaces the previous per
tags: []
---

# Browser — Headed Chrome Reuse Service

## What is it
A shared headed Chrome management service on agent-runner — a three-component architecture that enables one Chrome instance to be reused by all agents, replacing the previous N-fold memory waste of each agent spawning its own Chrome.

**Role affiliation**: agent-runner side (gateway does not run) — `ServiceSpec.capabilities=AGENT_RUNNER` in `services/supervision/roster/__init__.py`; the roster is derived by `services_for_capabilities` intersecting with the local `machine_role()`.

## Core Responsibilities
- **Shared Chrome instance**: `daemon.py` launches a headed Chrome with a CDP remote-debugging port and a dedicated persistent profile. `os.execvp` replaces the supervised session process with Chrome, so the session owns its native PID.
- **Browser liveness uses session and CDP evidence**: the healthcheck requires the session to remain live and Chrome to answer on the bound CDP port. It only reports health and ownership. A `SingletonLock` handoff can leave an orphan Chrome after the session exits; explicit stop uses the profile-scoped orphan sweep.
- **Shared MCP upstream**: `mcp_daemon.py` shares one `chrome-devtools-mcp` across agents. `gateway_session.py` logs in to the gateway, injects the opaque server session into Chrome over CDP, and refreshes it every six hours.
- **MCP process composition**: [[services/docs/agent_runner_side/browser/browser/process-composition.meiva.okf.md]] owns the captured image, live readers and lazy logging database.
- **Invariants**: serial lock (machine-wide, one browser operation at a time, no interleaving of multi-step sequences) + per-connection page affinity (each connection remembers its own current page; page-scoped calls first re-select before forwarding, so A's click won't land on B's tab) + bounded page lifetime (every page this stack creates carries a hard TTL deadline — 24h default, `renew_page` extends at most 24h per call)
- **Upstream failure**: one connection attempt per daemon process. Upstream death ends the process after cleanup; the root owns bounded process restart. Internal reconnect and page-affinity continuity are not guaranteed.
- **In-daemon MCP client**: `meiva/mcps/_daemon.py` dispatches `"shared": "browser"` to `meiva/mcps/_browser.py` without a bridge subprocess. `mcp_wrapper.py` remains a stdio entry for callers that request it. SDK v2 lowlevel `Server` forwards raw arguments; the upstream validates them. `mcp_socket_bridge.py` shares socket framing and retry code with the computer wrapper; browser sets its own rejection and close policies. P1 safety change: write/drain errors surface without retry because delivery may be unknown and a browser action could repeat.
- **Timeout ownership**: each wrapper owns a `ConfigBoot`. Its reader supplies the current timeout per socket request and survives reconnects; link construction does not read it. The upstream requires a reader at each initialize/stop race.
- **Strongly-typed line protocol**: `protocol.py` defines `Request`/`Response` (`OkResponse | ErrResponse`, discriminated by `ok`) TypedDict — three processes, daemon (server), wrapper (client), `healthchecks/browser_mcp.py` (probe), share the same wire types, no longer hand-writing dict shapes individually.

## Gating
See [[services/docs/agent_runner_side/browser/browser/gating.meiva.okf.md]]. Per-agent tab groups: [tab-groups](tab-groups.meiva.okf.md).

## Key Dependencies
- [[meiva/mcps/docs/mcps.meiva.okf.md]] — `chrome-devtools-mcp` is the upstream
- [[services/supervision/meiva_root_glue/docs/meiva_root_glue.meiva.okf.md]] — keeps alive via `healthchecks/browser.py` (identity-verified CDP **and** meiva-browser session liveness — CDP alone can tell neither a supervised Chrome from an orphan holding the port, nor ours from another unit's) and `healthchecks/browser_mcp.py` (Unix socket ping)

## Entry Points
- `services/desktop/browser/daemon.py` — Chrome launch (`MEIVA_BROWSER_ENABLED` **defaults to True**; `browser_incapability()` auto-gates machines lacking display/Chrome/npx)
- `services/desktop/browser/mcp_daemon.py` — shared MCP upstream
- `services/desktop/browser/mcp_wrapper.py` — per-agent MCP bridge
- `services/desktop/browser/mcp_socket_bridge.py` — socket client shared by browser and computer wrappers
- `services/desktop/browser/protocol.py` — line-protocol types shared by three processes
- `services/desktop/browser/orphan.py` — identify + reap a Chrome on this cluster's profile that left the session tree (called from `_do_stop(keep_browser=False)`)
- `services/desktop/browser/probe.py` — is the Chrome on this cluster's CDP port OURS (`DaemonProbe` verdict); the roster's `ServiceSpec.identity_probe` and `healthchecks/browser.py` both run it

## Notes
- **The profile is also how a probe knows the browser is ours**: CDP exposes no field we control — measured, `/json/version` carries only browser/protocol/UA/V8/WebKit strings plus a per-launch websocket uuid, and `DevToolsActivePort` is written only when the port is auto-assigned — so `probe.py` reuses `orphan.py`'s `--user-data-dir` token and adds the half a profile match alone cannot give: the identified Chrome must hold the LISTEN socket on the CDP port. Existing is not owning; on a box with two localhost namespaces our Chrome can be alive with a dead DevTools endpoint while a relayed one answers. The probe asks the question in BOTH directions — the walk direction above, plus a listener-first direction that reads the global TCP table for the pid owning the LISTEN socket and asks whether THAT pid is ours (by the walk, or by its own argv). Either may win; the listener-first arm exists because the walk direction alone has blind spots (an unreadable argv, a failed socket read) that would misread our own listener as a foreign occupant.
- **An unsupervised Chrome requires operator cleanup**: the healthcheck reports a missing supervised session or an unhealthy CDP endpoint without sweeping or respawning Chrome. Root supervision retains its bounded process restart policy. `meiva stop` uses `orphan.reap_cluster_chrome` to clean up this cluster's profile before an explicit start.
- **The healthcheck's ERROR lines are episode-gated**: one line per failure episode + a 6h reminder; quiet rounds log DEBUG ([[services/supervision/healthchecks/docs/terminal-verdict/terminal-verdict.meiva.okf.md]]).
- **A teardown reaches Chrome by profile, not by process tree**: killing the `meiva-browser` session cannot reach a Chrome that left the tree on a `SingletonLock` handoff, so `meiva stop` / `meiva cluster destroy` additionally name Chrome by this cluster's own `--user-data-dir` and kill it. Identification, the argument it cannot select the operator's browser, scope and ordering: [[services/docs/agent_runner_side/browser/browser-teardown.meiva.okf.md]]
- `MEIVA_BROWSER_ENABLED` **defaults to True** (not opt-in) — auto-detects host capability (display + Chrome + npx); if unavailable, `browser_incapability()` automatically skips (`base/host/system/probes.py:123`; applied as a service gate in `services/supervision/roster/__init__.py`)
- Chrome profile is persistent, retaining login state
- **No business database dependency**: CDP and Unix requests retain `requires_db=False`; optional logging dials lazily without adding a startup admission gate. See the process composition owner above.

Profile provisioning and operator remedies are in [browser operations](../../../../desktop/browser/docs/operations.md).
