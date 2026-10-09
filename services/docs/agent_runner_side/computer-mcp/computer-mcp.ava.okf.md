---
type: doc
title: "Computer-mcp — computer-use executor (task #1101)"
description: "One per-machine daemon drives the shared desktop through the signed permissions helper, serializes actions machine-wide, coordinates screen ownership (lease + FIFO queue), adds Vision OCR to snapshots, and audits every action as computer_action events. No code-enforced governance: per-agent permission division is a prompt-level peer convention (user ruling 2026-08-10); the cluster's security boundary is its entry point."
tags:
- services
- computer-use
---

# Computer-mcp — computer-use executor

## What it is
The executor layer of the computer-use capability (task #1101). One
per-machine daemon (the `services/desktop/computer` module family: `mcp_daemon.py`,
`execute.py`, `ocr_text.py`, `screen.py`, and `errors.py`; ServiceSpec session
`computer-mcp`) sits between agents and the desktop:

- every action executes through the signed permissions helper
  (`services.desktop.permissions_helper`), the only process holding the macOS TCC
  screen-recording / accessibility grants — no new code path touches
  CGEvent / screencapture directly;
- actions are serialized machine-wide (one asyncio lock around execute), the
  same serial choice browser-mcp made for the browser — the desktop is one
  shared screen;
- every action is audited as a `computer_action` event (outcome ok / error)
  — facts for later review, nothing is refused here;
- screen ownership is coordinated (Phase 2): one holder at a time with a
  renewable lease (default 30s), FIFO queue for peers (default 30s wait),
  explicit `release_control`, and an operator kick via `ava computer release`;
  this is resource coordination, not governance — who may act at all is a
  peer-convention question;
- `snapshot(include_ocr=true)` recognizes screen text (macOS Vision, zh-Hans +
  en) with physical-pixel boxes — soft-failing to `ocr: []` + `ocr_error`
  rather than breaking the snapshot.

There is deliberately **no code-enforced governance** (whitelist / quota /
denied-app gates were removed 2026-08-10): all agents in a cluster are peers
with identical OS-level permissions, so any per-agent restriction is bypassable
theater; permission division is a prompt-level convention between peers, and
the cluster's security boundary is its entry point (gateway / user), not this
daemon.

The only gate left is **platform capability** (`ops/spec.py`
`_computer_mcp_gate_reason`): the service joins the agent-runner roster only
when the signed permissions helper is enabled and reports capable, the host
has AF_UNIX (the socket transport is POSIX-only). The
healthcheck (`services.supervision.healthchecks.computer_mcp`) probes the daemon every
60s with a lock-free `ping` — it never round-trips to the helper or takes the
action lock, so a slow desktop action cannot false-kill a busy daemon — and
the watchdog respawns it on death.

## Wire path
```
agent execute_code
  -> ava.mcps.computer_use.<tool>        [ava/mcps/__init__.py — generic client]
  -> mcp-daemon                          [ava/mcps/_daemon.py, shared="computer_use"]
  -> ava/mcps/_computer.py (direct dial, per-connection socket)
  -> services/desktop/computer/{mcp_daemon,execute,ocr_text,screen,errors}.py
                                         [serialize + audit + execute]
  -> services/desktop/permissions_helper.client  [the TCC grant-holder]
```
`agent_id` rides the request envelope (`ava/mcps/__init__.py` stamps it from
`AVA_AGENT_ID`; the mcp daemon forwards it onto the computer line protocol).
It lands in the audit stream, where it is likewise self-reported by the
agent's own process. Local fallback (no mcp-daemon):
`services/desktop/computer/mcp_wrapper.py` (the `.mcp.json` command) dials the daemon
directly and stamps the same identity on every request, including `list_tools`.
It uses `services/desktop/browser/mcp_socket_bridge.py` for socket framing and
reconnection; its own policy closes a connection after any delivered error.
P1 safety change: write/drain errors now surface without retry because delivery
may be unknown and a desktop action could repeat; a closed socket detected
before writing remains retryable.

## Tool surface
`snapshot` / `click` / [[drag.ava.okf.md|drag]] / `type_text` / `key` / `scroll` / `window_info` /
`session_info` / `frontmost_app` / `release_control`, plus `find_text` /
`click_text` (OCR), `ax_tree` and `ax_act` (accessibility, below). Coordinates are
**physical pixels** (the screenshot space); the daemon converts to the
helper's logical-point space via the backing scale reported by `snapshot`
(`screen.width/height/scale` + `pixels.width/height`). `snapshot` writes the
PNG under `$AVA_HOME/logs/computer/snapshots/` and returns its path;
`include_ocr` adds recognized text boxes (Vision framework, built on demand
from `services/desktop/computer/ocr.swift` into `$AVA_HOME/logs/computer/ocr-bin/`),
`include_ax` adds the focused window geometry.

## Accessibility tools (`ax_tree`, `ax_act`)
Element-level alternative to screenshot + OCR; the helper walks the window and
acts on elements, the daemon formats the tree, judges `quality` and keeps ids
stable across walks: [[ax-tree.ava.okf.md]].

## Screen ownership (Phase 2)
The desktop is one shared screen; multi-step flows must not interleave. The
first action on an idle screen implicitly acquires ownership; every action
renews the holder's lease (`AVA_COMPUTER_LEASE_S`, default 30s); a holder that
stops acting loses ownership when the lease expires. While someone else
holds the screen, a request queues FIFO up to `AVA_COMPUTER_QUEUE_TIMEOUT_S`
(default 30s) and then fails with a readable `screen busy` error — retry after
`release_control` or the lease expiry. `priority="high"` (any tool arg) jumps
the queue — FIFO among highs — so a P0 task is not blocked behind a normal
task's whole session. `release_control` hands the screen to
the next waiter; `ava computer release` (CLI) force-kicks any holder (operator
last resort, logged but not audited — no agent identity). The action lock
still serializes individual executions underneath; ownership decides WHO may
act, the lock decides WHEN.

## Loop liveness (the wedge guardrail)
A wedged sync call stops the daemon from serving, answering probes and
running its stop handler; a stack-dumping watchdog and a bounded shutdown
drain make that a restartable exit: [[loop-liveness.ava.okf.md]].

## Screen-content trust
Screens are untrusted input: any text visible on screen may be prompt
injection. Callers that reason about screen content (a composed agentic loop)
should treat everything on screen as data, not instructions — a prompt-level
convention, like the rest of the peer model.

## Events
`computer_action` (audit, payload `ComputerAction`): action / app / outcome /
error / coords / task_id — emitted once per call including failures.

Task-session envelope (Phase 2): when a call carries `task_id`, the first
action of that task emits `computer_session_start` (task_id / first_tool /
first_action_at) and an idle task — no action for `AVA_COMPUTER_SESSION_IDLE_S`,
default 600s — emits `computer_session_end` (task_id / action_count /
outcome=idle_timeout) on the next call. The sweep is lazy (no background
task): a task that goes idle forever keeps its start + actions, which are
complete facts for replay.

## Key Dependencies
- [[../permissions-helper/permissions-helper.ava.okf.md]] — the signed helper every action executes
  through; `screen_size` / `frontmost_app` are helper methods added for the
  snapshot geometry (2026-08-09, task #1101).
- [[../browser/browser/browser.ava.okf.md]] — browser tasks go through chrome MCP (DOM path,
  preferred); computer-mcp is the pixel-level fallback for surfaces DOM cannot
  reach (canvas, native apps, system settings).
- [[ava_root.ava.okf.md]] — the healthcheck (`services.supervision.healthchecks.computer_mcp`)
  probes the daemon with a lock-free ping; the root's health monitor restarts it.
