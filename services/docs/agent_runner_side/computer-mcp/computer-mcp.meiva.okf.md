---
type: doc
title: "Computer-mcp — computer-use executor (task #1101)"
description: "Per-machine desktop executor via the signed helper, with serialized actions, renewable screen ownership, OCR/AX observations and audits."
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
  explicit `release_control`, and an operator kick via `meiva computer release`;
  resource coordination, not governance (who may act is a peer convention);
- `snapshot(include_ocr=true)` recognizes screen text (macOS Vision, zh-Hans +
  en) with physical-pixel boxes — soft-failing to `ocr: []` + `ocr_error`
  rather than breaking the snapshot.

**No code-enforced governance**: whitelist, quota and denied-app gates were
removed 2026-08-10. Agents have identical OS permissions; permission division
is a peer convention. The gateway/user entry point owns the security boundary.

The only gate left is **platform capability** (`services/supervision/roster/spec.py`
`_computer_mcp_gate_reason`): the service joins the agent-runner roster only
when the signed permissions helper is enabled and reports capable. The
healthcheck (`services.supervision.healthchecks.computer_mcp`) probes it every
60s with a lock-free `ping` — it never round-trips to the helper or takes the
action lock, so a slow desktop action cannot false-kill a busy daemon — and
the watchdog respawns it on death.

Entry-owned image and database: [[process-composition.meiva.okf.md]].

## Wire path
```
agent execute_code
  -> meiva.mcps.computer_use.<tool>        [meiva/mcps/__init__.py — generic client]
  -> mcp-daemon                          [meiva/mcps/_daemon.py, shared="computer_use"]
  -> meiva/mcps/_computer.py (direct dial, per-connection socket)
  -> services/desktop/computer/{mcp_daemon,execute,ocr_text,screen,errors}.py
                                         [serialize + audit + execute]
  -> base/host/permissions_helper/client  [the TCC grant-holder]
```
`agent_id` rides the request envelope (`meiva/mcps/__init__.py` stamps it from
`MEIVA_AGENT_ID`; the mcp daemon forwards it onto the computer line protocol).
It lands in the audit stream, where it is likewise self-reported by the
agent's own process. Local fallback (no mcp-daemon):
`services/desktop/computer/mcp_wrapper.py` dials the daemon
directly and stamps the same identity on every request, including `list_tools`.
It uses `services/desktop/browser/mcp_socket_bridge.py` for socket framing and
reconnection; its own policy closes a connection after any delivered error.
P1 safety change: write/drain errors now surface without retry because delivery
may be unknown and a desktop action could repeat; a closed socket detected
before writing remains retryable.

## Tool surface
`snapshot` / `click` / [[drag.meiva.okf.md|drag]] / `mouse_down` / `mouse_up` / `move` / `cursor_position` /
`type_text` / `key` / `scroll` / `list_apps` / `list_windows` / `focus_app` / `window_info` /
`session_info` / `frontmost_app` / `release_control`, plus `find_text` /
`click_text` (OCR), `clipboard_read` / `clipboard_write`,
`ax_tree` and `ax_act` (accessibility, below). Coordinates are
**physical pixels** (the screenshot space); the daemon converts to the
helper's logical-point space via the backing scale reported by `snapshot`
(`screen.width/height/scale` + `pixels.width/height`). `snapshot` writes the
PNG under `$MEIVA_HOME/logs/computer/snapshots/` and returns its path;
`include_ocr` adds recognized text boxes (Vision framework, built on demand
from `services/desktop/computer/ocr/ocr.swift` into `$MEIVA_HOME/logs/computer/ocr-bin/`),
`include_ax` adds the focused window geometry.

Explicit observation selectors, cropped captures and coordinate frames are owned
by `targets.py` and `screen.py`: [[observation-frames.meiva.okf.md]].
Strict input contracts and generated schemas: [[native-input.meiva.okf.md]];
clipboard: [[clipboard.meiva.okf.md]]; held mouse buttons: [[mouse-hold.meiva.okf.md]].

## Accessibility tools (`ax_tree`, `ax_act`)
Element-level alternative to screenshot + OCR; the helper walks the window and
acts on elements, the daemon formats the tree, judges `quality` and keeps ids
stable across walks: [[ax-tree.meiva.okf.md]].

## Screen ownership (Phase 2)
The desktop is one shared screen; multi-step flows must not interleave. The
first action on an idle screen implicitly acquires ownership; every action
renews the holder's lease (`MEIVA_COMPUTER_LEASE_S`, default 30s); a holder that
stops acting loses ownership when the lease expires. While someone else
holds the screen, a request queues FIFO up to `MEIVA_COMPUTER_QUEUE_TIMEOUT_S`
(default 30s) and then fails with a readable `screen busy` error — retry after
`release_control` or the lease expiry. `priority="high"` (any tool arg) jumps
the queue — FIFO among highs — so a P0 task is not blocked behind a normal
task's whole session. `release_control` hands the screen to
the next waiter; `meiva computer release` (CLI) force-kicks any holder (operator
last resort, logged but not audited — no agent identity). The action lock
still serializes individual executions underneath; ownership decides WHO may
act, the lock decides WHEN.

## Loop liveness (the wedge guardrail)
A wedged sync call stops the daemon from serving, answering probes and
running its stop handler; a stack-dumping watchdog and a bounded shutdown
drain make that a restartable exit: [[loop-liveness.meiva.okf.md]].

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
first_action_at) and an idle task — no action for `MEIVA_COMPUTER_SESSION_IDLE_S`,
default 600s — emits `computer_session_end` (task_id / action_count /
outcome=idle_timeout) on the next call. The sweep is lazy (no background
task): a task that goes idle forever keeps its start + actions, which are
complete facts for replay.

## Key Dependencies
- [[../permissions-helper/permissions-helper.meiva.okf.md]] — the signed helper every action executes
  through; `screen_size` / `frontmost_app` are helper methods added for the
  snapshot geometry (2026-08-09, task #1101).
- [[../browser/browser/browser.meiva.okf.md]] — browser tasks go through chrome MCP (DOM path,
  preferred); computer-mcp is the pixel-level fallback for surfaces DOM cannot
  reach (canvas, native apps, system settings).
- [[meiva_root.meiva.okf.md]] — the healthcheck (`services.supervision.healthchecks.computer_mcp`)
  probes the daemon with a lock-free ping; the root's health monitor restarts it.
