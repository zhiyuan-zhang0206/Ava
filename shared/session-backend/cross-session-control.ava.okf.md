---
type: doc
title: "Windows cross-session control — the resident session steward"
description: "How a graceful stop from any entry reaches a Windows service in another session: winproc.new_session co-locates a resident control steward with every session, and graceful_signal routes by caller/target session — same-session direct console attach, cross-session via the steward's AF_UNIX socket. Refusals are explicit, never force escalations."
tags:
- shared
- process
- supervision
- windows
---

# Windows cross-session control

## What it is

`AttachConsole` cannot cross a Windows session boundary (SSH entry runs in
session 0, desktop-started services in session 1 — machine observations, never
product constants), so the one-shot private-console helper can only deliver
Ctrl-Break to a target in the caller's own session. The fix solves the problem
at *launch* time: every `winproc.new_session` also spawns a resident control
steward in the same session as the target (the spawner's own session), and
`winproc.graceful_signal` compares the caller's and target's sessions
(`shared/windows_session.py`, `ProcessIdToSessionId`) to pick the channel:

- **same session** — the direct one-shot helper (`shared/windows_console_signal.py`,
  unchanged, battle-tested since the 2026-09-07 deploy);
- **different session** — send `break` over the steward's AF_UNIX control
  socket; the steward runs the same verified helper from inside the target's
  session.

The socket path is `$AVA_HOME/run/ctrl/ava-ctrl-<pid>-<create_time>.sock` —
the record's exact identity embedded in the endpoint, so a caller can only
address a steward by knowing the verified record. The steward is a *sibling*
of the target (both children of the spawner), never part of the target's tree,
so a tree kill never reaches it; `winproc._spared_pids` spares recorded
stewards the way it spares recorded sessions.

## Refusals never escalate

A cross-session target with no steward (legacy record predating this channel,
or a dead steward), a steward that refuses delivery, a timeout, or an
undeterminable caller session all raise — the stop flow reports an incomplete
stop and the operator decides; there is no hidden force-kill. `list_sessions`
reaps a crashed steward's socket alongside its dead record. `control_mode`
stays `private-console-v1`; the new `steward_pid` / `steward_socket` record
fields are optional, so old records keep same-session control and old/new code
roll forward cleanly.

## Headless behavior

Sessions run in whatever session started them — nothing is forced into session
1, and the control channel needs no interactive logon, so agent-host / ops /
updater run normally with nobody logged in. The one GUI-dependent piece, the
permissions helper, already registers an ONLOGON interactive task; its
converge reports "no interactive session" plainly instead of silently
no-opping when nobody is logged on.

## Entry points

- `shared/winproc.py:new_session` / `_spawn_steward` — steward launch + record fields
- `shared/winproc.py:graceful_signal` / `_steward_deliver` — session routing + socket transport
- `shared/windows_session.py` — `ProcessIdToSessionId` / `WTSGetActiveConsoleSessionId` wrappers
- `shared/windows_session_steward.py` — the resident leaf (isolated `-I`, stdlib + psutil only)

## The helper's Job containment

`shared/winjob_spawn.py` starts only this temporary helper using Windows 10+
`PROC_THREAD_ATTRIBUTE_JOB_LIST`: redirector and future interpreter join the
existing close-to-kill Job before any thread executes. No inherited Job handle
or late self-enrollment keeps the family alive after timeout/owner death. The
helper rechecks its fixed deadline before control delivery. Unsupported
process attributes or Job restrictions refuse before helper execution; no
system-interpreter or uncontained subprocess fallback exists. This mechanism
does not establish a retained Windows base-Python release image.

## Notes

- Successful delivery means Windows accepted the request, not that the target
  exited — the same contract as the direct helper.
- The steward logs only identity/lifecycle lines to `$AVA_HOME/logs/<name>.ctrl.log`.
- Session numbers change across logins; this module only ever *compares* them.
