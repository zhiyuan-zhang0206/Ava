---
type: doc
title: ava.ui — User Interface
description: '`ava.ui` allows agents to display rich web pages and notifications to users. The agent runs its own HTTP server, and the UI system presents the link to the user.'
tags:
- agent-view
- sdk
- agent-lifecycle
---

# ava.ui — User Interface

## What it is

`ava.ui` allows agents to display rich web pages and notifications to users. The agent runs its own HTTP server, and the UI system presents the link to the user. The page server binds the machine's reachable host (loopback on a single box) and is served to the user through the **gateway reverse proxy** — the link is the gateway's own authenticated URL (`/api/agents/<id>/pages/<name>/`), so the browser never dials the page server directly.

## Core API (core SDK: page serving)

Each agent has a default port reserved for itself (derived from agent id), used if `port` is omitted; only one active page is allowed at a time (opening a new one auto-closes the old).

- `serve(dir, name, port=None, title=None) → Page` — One-step: start directory HTTP server + register with UI, poll `/health` until listening. If `port` omitted, use default reserved port. A relative `dir` resolves against the agent's working directory (`ava.cwd`), consistent with the `ava.files` API.
- `serve_markdown(content, name, port=None, title=None) → Page` — Serve-side render Markdown as an HTML page with LaTeX math, code highlighting, GFM tables (port semantics same as serve).
- `show(name, port=None, title=None) → Page` — Register an already running HTTP server (does not verify port occupation).
- `close(name)` — Deregister page and kill the server started by `serve()`.

`name` must match `^[a-zA-Z0-9_-]+$` (1-64 chars). Returned `Page`: id, name, port, title, url.

### Port-occupancy contract
`serve` probes the port on the **reachable host** (not the wildcard — a specific-address occupant would be missed by a `0.0.0.0` probe on macOS). Three cases:

1. **Own orphan** (a page server this agent started in an earlier run, surviving a restart — identified by its `ava_ui_` server-script marker in the command line): reclaimed automatically, then the new server starts. This is why a re-serve after restart works without manual session cleanup.
2. **Another process** (no `ava_ui_` marker): `serve` **directly errors, never forcibly kills the occupier** (error message includes occupier pid/cmdline). Re-serve path: kill the shell session holding the old server (`ava.shell.sessions.list()` then `ava.shell.sessions.kill(id)`; if the old page is still within this process, use `ava.ui.close(name)`), or use a free `port`.
3. **Stale server answering `/health`**: the liveness poll only accepts `ok:<per-launch token>` from the script just written — a stale occupant answering plain `ok` can never satisfy it, so `serve` can never report success against old content.

Each `serve` embeds a random token in the server script; `/health` echoes it. The poll loops until the token matches (or 6s timeout → `PageError`), so startup confirmation is always about *this* launch, never about whatever happens to be on the port.

## ava_fleet Plugin Injections (Notifications)

The following are mounted onto the `ava.ui` namespace by the `ava_fleet` plugin (non-core). See plugin-side OKF for details.

- `notify(title, content=None, *, require_response=False, blocking=False, priority='P2') → Notice` — Push a notification to the user, replacing your previous active notification (max one per agent). `require_response=True` requests a reply; `blocking=True` indicates you are blocked waiting. The returned `Notice` is an int subclass with `.pending_count` / `.pending_notices` / `.superseded`.
- `edit_notice(*, title=..., content=..., priority=..., blocking=...)` — Edit the active notification (only pass fields to change; cannot change `require_response`; at most one is open, so no id).
- `dismiss_notice()` — Dismiss the active notification (at most one is open, so no id).

### Priority
P0 (highest) → P1 → P2 (default) → P3 (lowest). This is advisory—does not affect processing order.

## Key Dependencies
- [[ui/web/src/frontend.ava.okf.md]] — Frontend UI renders pages and notifications

## Notes
`notify` is the only reliable channel for agents to reach users, with **queue** semantics—users consume at their own pace, delivery is delivery. Use `require_response=True` when user decision is needed (irreversible operations, spending money, contacting real people)—authorization/decision goes directly to the user from any depth; progress/conclusions are first reported to the delegator for aggregation (fleet communication dichotomy), and agent-to-agent matters use `send_message`. Out-of-band pushes (e.g., telegram) are only allowed for truly urgent matters, discipline is governed by the disturbance discipline skill.
