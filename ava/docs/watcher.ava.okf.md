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

A watcher is nothing more than a shell session running a generated script —
there is no separate registry, desired-state record, or boot reconcile
(docs/decisions/runtime/updates/recovery/2026-09-27-watchers-are-never-restarted.md). Everything that
applies to an ordinary `ava.shell.sessions` session applies to a watcher's
session too: list it, capture its output, renew its TTL deadline, or kill it.
Nothing ever restarts a watcher automatically — write watcher scripts
assuming they may be cut off at any moment and will not be re-run for you.

## Core API

- `at(when, message, *, name, notify=None) → int` — One-time scheduled wake-up. `when` supports TZ-aware datetime / timedelta / ISO-8601 strings. Returns session ID (can be cancelled with kill).
- `cron(expr, message, *, timezone=None, end_time=None, name, notify=None) → int` — Periodic wake-up via 5-field cron expression. `end_time` sets expiry; it **defaults to now + 7 days** (standing cap, task #2617) — a longer schedule passes an explicit `end_time`. Calling `cron()` again with the same expression + timezone does **not** replace or renew anything — it starts another, independent session; kill the old one yourself (`ava.shell.sessions.kill`) if you don't want both running. Returns session ID.
- `launch(code, timeout, *, name, notify=None) → int` — Run custom Python code as watcher. Inside the code, call `ava.agents.send_message(ava.self.AGENT_ID, content)` to wake you. Force-stop after `timeout` (exit code 124).

Omitted `notify` marks the shell-level send as a plain platform completion (`ava/shell/background.py:notified_line`); the gateway resolves it against the target agent's `completion_notice_policy` (`all` default, or `hourly`) at delivery time, not at spawn — `hourly` retains every completion in its digest. Explicit `notify="always"` is baked into the shell command at spawn and overrides the policy for that one run.

## What happens when a watcher's session ends

- **The watcher exits on its own** (script finished, one-shot fired, timeout watchdog) — the shell layer sends the usual completion notice (log path, last 3 output lines), respecting the `notify` policy, and the session closes itself. Nothing else to do; re-launch it yourself if you still need it.
- **You kill it yourself** (`ava.shell.sessions.kill` / `kill_all`) — no extra message: the call already returned its result to you in the same turn.
- **The platform reclaims it at its TTL deadline** (`ava.shell.sessions`' TTL reaper — a watcher's shell TTL IS its target deadline: launch = created + timeout, cron = end, at = moment + grace) — you get the ordinary shell-reclaimed interruption notice, because a watcher always has a running job when this fires.
- **A normal `ava stop` or `ava restart` (updates included) force-closes busy terminals** — you get the closure notice for a busy session, written by the stop itself (`cli/commands/lifecycle/service_stop.py`), the same as any other shell with work in flight.
- **`ava stop --force` (`cli/commands/lifecycle/service_stop.py:force_close_terminals`), a Windows unit's stop, or the pty-sessions service ending with no orchestrated closer** (an external SIGKILL, a machine power loss, a service crash with nothing to record it) — accepted gap: you get no notice in any of these. The session is simply gone from `ava.shell.sessions.list()` on your next check. Nothing is watching for these cases, by design (see the decision record for why); if a watcher's absence would matter, check for it explicitly rather than relying on a message.

None of these ever re-spawns the watcher. Decide whether to re-create it yourself.

**A watcher outlives its owner's termination.** Terminating the agent does
nothing to a watcher it left running — it is a session, not part of the
agent's process. The watcher's next fire delivers its wake as an ordinary
chat inbound (`source="watcher:<id>"`), and chat delivery auto-resurrects a
terminated agent (`gateway/agents/delivery.py` ->
`ops.resurrect_if_terminated`) — nothing reaps a terminated owner's watcher
early any more, so a standing cron keeps re-waking it at every fire for as
long as it lives. This is intended: the LLM decides each time. An agent that
does not want to be woken again kills its watchers
(`ava.shell.sessions.kill`) before terminating.

**Orphan governance (task #1726).** A watcher whose session ended without taking it along (a pty-sessions service crash, an external SIGKILL of the shell) is reparented to init with its session gone — still alive, still firing cron/at; 49 of 85 watcher processes on the fleet host were once such multi-generation orphans. The generated bootstrap arms an **orphan guard**: a daemon thread comparing `os.getppid()` against the boot-time parent every 5s, hard-exiting with code 125 on a mismatch, so session end → child death within seconds on every such path. This is the only thing standing between a dead session and a watcher that keeps firing forever — nothing external scans for or kills orphaned watcher processes any more.

## Key Dependencies
- [[shell.ava.okf.md]] — a watcher's underlying session IS an `ava.shell.sessions` session: `ava/watcher.py:_spawn` uses `ava.shell.sessions.create_session` + `ava/shell/background.py` for notification (unrelated to `services/watchdog`)
- `base/host/env/dotenv_boot.py:watcher_runner_env` validates an agent-profile launcher's runner-class DB login (with its `AVA_DB_GENERATION` marker when delivered) and runner Redis URLs before session allocation and passes them only to the watcher session. A secured default-home gateway refuses a launcher without that projection before creating a watcher; other homes and pure agent-runner units retain their child config source. The data-plane owner-URL guard remains the child-side boundary.

## Notes
Generated watcher scripts (`watcher_<session_id>.py` + `_boot.py` + `_launch.sh`) land under `$TMPDIR/ava/<cluster>/<agent>/watchers/` (`ava/watcher.py:_watchers_dir`) — temp storage because they are read exactly once at launch; the per-cluster + per-agent path segments keep co-located clusters (each with its own DB-assigned session ids, which may overlap) from colliding. The generated script files only need to survive launch — there is no other persistence, and liveness is the session itself. While running, a watcher is a normal shell session—visible in `ava.shell.sessions.list()`. Timeout parameter supports seconds, `timedelta`, or `"<n>{s,m,h,d}"` strings. When a watcher stops (self-exit / crash / timeout), the shell layer sends the policy-selected completion notification (exit code + log path of full output + output tail), then the session auto-closes—even if the child process gets SIGKILL, an eligible notification is not lost. The dedicated `remind` primitive has been removed: waking yourself = sending yourself a message (generic `ava.agents.send_message`); at/cron generated scripts internally use the same delivery path (source marked `watcher:N`).
