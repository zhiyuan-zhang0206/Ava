---
type: doc
title: "Stopping a process — the kill contract and the non-session trio"
description: "How Ava stops what it started: `kill_session`'s (ok, mode) contract and its graceful/forced escalation for named sessions, and `shared/proc.py`'s `process_alive` / `request_stop` / `force_kill` trio for the processes that are not sessions (pooler, port orphans, the gate daemon) — including why the obvious POSIX spellings do not survive the crossing to Windows."
tags:
- shared
- process
- supervision
- stop
---

# Stopping a process

## What it is

The two ways Ava ends a process it started. A **named session** goes through its
backend's `kill_session`; everything else — a pooler, an orphan holding a unit
port, the gate daemon — goes through the platform-neutral trio in
`shared/proc.py`. They are one topic because they answer the same question with
the same discipline: ask first, escalate second, and report what actually
happened rather than what was requested.

## Core responsibilities

### Kill contract

`kill_session(name, graceful=...)` → `(ok, mode)` with mode in `{graceful, forced, noop}`. `mode` reports what **happened**, not what was requested: a graceful stop that had to escalate to the SIGKILL fallback returns `forced`, so the caller's escalation marker fires instead of a clean-stop one that hides a hard kill. Idempotent: an absent/dead session is a `noop`. `ok` means **the session is confirmed gone**, not "the kill command was accepted" — backends re-ask their own existence check after killing, because a kill that reports success it did not achieve turns a live-but-unbacked session into a service nothing starts (issue #1015). `graceful=True` SIGTERMs only the top process and waits up to the timeout, then hard-kills the tree; `graceful=False` SIGKILLs children first so a parent cannot respawn a child mid-teardown. `expected=True` marks an operator-initiated transition (rollout/update/stop) so backends that escalate a kill log at INFO instead of WARNING/ERROR there.

A graceful stop only means anything if the signal reaches the process that can act on it: the recorded pid must BE the daemon, not a wrapper shell in front of it — see the `new_session` contract in [[session-backend.ava.okf.md|session backend]] for the two `exec`s that guarantee it, and `shared/daemon_shutdown.py` for the handler that turns the delivered SIGTERM into the daemon's own unwind.

### Stops that do not go through a session

Not every process Ava stops is a named session: the pooler, an orphan holding a unit port, the gate daemon. Those go through `shared/proc.py`'s trio — `process_alive` (probe) / `request_stop` (ask) / `force_kill` (force) — and **must**, because two of the obvious spellings do not survive the crossing to Windows: `os.kill(pid, 0)` *terminates* the target there rather than probing it, and `signal.SIGKILL` is undefined. `cli/commands/_pgbouncer.py:_terminate_verified` is the escalating stop built on them (ask -> poll -> force -> verdict), reached from `_do_stop` on every platform. A pid this user may not signal is handled the same way on all three legs: alive, undeliverable, reported as a survivor — never an exception out of the middle of a stop. Same file: `kill_process_tree` (parent + descendants, enumerated before the kill) and `run_bounded` (a timeout that bounds the work, not just the wrapper).

## Entry points

- `shared/session_backend.py:SessionBackend.kill_session` — the session stop, per backend
- `shared/proc.py:process_alive` / `request_stop` / `force_kill` — the non-session trio
- `shared/proc.py:kill_process_tree` / `run_bounded` — tree teardown and a bounded run
- `cli/commands/_pgbouncer.py:_terminate_verified` — the escalating stop built on the trio

## Notes

- The trio is the reason a stop path can be written once and run on all three
  platforms: the Windows divergences live inside it, not at every call site.
