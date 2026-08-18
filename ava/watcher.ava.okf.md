---
type: doc
title: ava.watcher — Background Listener
description: '`ava.watcher` starts background processes to wake you when specific conditions trigger—avoid polling. Three modes: time-based (`at`/`cron`), custom code (`launch`).'
tags:
- agent-view
- sdk
- agent-lifecycle
---

# ava.watcher — Background Listener

## What it is

`ava.watcher` starts background processes to wake you when specific conditions trigger—avoid polling. Three modes: time-based (`at`/`cron`), custom code (`launch`).

## Core API

- `at(when, message, *, name) → int` — One-time scheduled wake-up. `when` supports TZ-aware datetime / timedelta / ISO-8601 strings. Returns session ID (can be cancelled with kill).
- `cron(expr, message, *, timezone=None, end_time=None, name) → int` — Periodic wake-up via 5-field cron expression. `end_time` sets expiry. Returns session ID.
- `launch(code, timeout, *, name) → int` — Run custom Python code as watcher. Inside the code, call `ava.agents.send_message(ava.self.AGENT_ID, content)` to wake you. Force-stop after `timeout` (exit code 124).
- `reconcile() → list[str]` — called at agent boot (`agent/loop.py`): rebuilds watchers whose sessions were killed out from under them — `ava stop`'s reap, a crashed host, a machine reboot (the #1014 fix; rollouts no longer kill sessions) — and returns the action sentences.

## Watcher Registry & Boot Reconcile

Every spawn writes a row to the **`agent_watchers` registry** ([[shared/watcher_registry.ava.okf.md|registry API]], R1) carrying the kind's rebuild payload — the exact arguments `at`/`cron`/`launch` need to re-spawn. A watcher that exits **cleanly deletes its own row** (the generated bootstrap's finally); a **killed** watcher (`ava stop` reap / host crash / SIGKILL) keeps it — `sessions.kill` deletes deliberately killed ones, so only a genuine surprise survives.

At boot, `reconcile()` reads "row exists but session missing": standing `cron` schedules are re-spawned from the stored expression (marked `rebuilt`); `at` one-shots are rebuilt while their moment is still ahead, otherwise marked `missed` + alerted; `launch` one-shots are marked `missed` + alerted (never re-run — their work is time-bound). Fail-soft throughout: a registry failure never blocks the boot or the watcher it observes.

## Key Dependencies
- [[shell.ava.okf.md]] — watcher underlying is a shell session: `ava/watcher.py:_spawn` uses `ava.shell.sessions._create_session` + `ava/shell/_background.py` for notification (unrelated to `services/watchdog`)

## Notes
Generated watcher scripts (`watcher_<session_id>.py` + `_boot.py`) land in `$AVA_HOME/watchers/` (`ava/watcher.py:_watchers_dir`), not a global temp dir—a home owns everything under it, co-located clusters each have their own `$AVA_HOME` + DB-assigned session ids (which may overlap), so they won't collide on shared temp paths. The generated script files only need to survive launch — persistence is the `agent_watchers` registry, liveness is the session itself. While running, a watcher is a normal shell session—visible in `ava.shell.sessions.list()`. Timeout parameter supports seconds, `timedelta`, or `"<n>{s,m,h,d}"` strings. When a watcher stops (self-exit / crash / timeout), a exit notification is sent from the shell layer (exit code + log path of full output + output tail), then the session auto-closes—even if the child process gets SIGKILL, the notification is not lost. The dedicated `remind` primitive has been removed: waking yourself = sending yourself a message (generic `ava.agents.send_message`); at/cron generated scripts internally use the same delivery path (source marked `watcher:N`).
