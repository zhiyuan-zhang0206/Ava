---
type: doc
title: "Session backend & process supervision"
description: "How Ava supervises long-lived processes: the `shared/session_backend.py` interface, its three entry points (`get_backend()` for services + orchestration, `get_shell_backend()` for agent shells/watchers, `native_proc()` for agent processes), the backends (posixproc / per-session pty hosts / winproc), and the macOS firewall audit. Stopping is its own node."
tags:
- shared
- process
- supervision
---

# Session backend & process supervision

## What it is

`shared/session_backend.py` is the interface every long-running named session goes through. It unifies three platform supervisors behind one protocol: **posixproc** (`shared/posixproc.py`, POSIX native), the **per-session pty hosts** (`shared/pty_sessions/`), and **winproc** (`shared/winproc.py`, Windows). The `SessionBackend` surface: `has_session` / `new_session` / `kill_session` / `list_sessions`, plus optional `session_started_at` (uptime; a backend without a timestamp source answers None and consumers render no uptime) and `session_log_path` (the file this backend redirects output to — asked by liveness-from-freshness consumers such as `ops.cluster_deploy._reap_stalled_updater`). PTY-only ops (`send` / `send_keys` / `capture_pane`) raise `NotImplementedError` on backends without a terminal.

Three entry points, three session classes:

- `get_backend()` — the **service/daemon + orchestration** backend: `ava start` launches, healthcheck respawns, pause/unpause, and the updater / rollout / cluster-restart orchestration sessions (S7 moved them onto this backend). `PosixProcSessionBackend` on POSIX, `WinprocSessionBackend` on Windows.
- `get_shell_backend()` — **agent interactive shells / watchers**: `PtySessionBackend` (per-session pty hosts) on POSIX, the native supervisor on Windows. Never addresses service or orchestration sessions.
- `native_proc()` — **agent processes** (non-interactive, no PTY needed): the posixproc or winproc module directly, used by `ops.agent_launch` (spawn / kill-stale) and the reap / force-terminate / status consumers.

**Every platform-supervisor import in this module is method-local, and that is load-bearing**: the agent-runner self-update (`cli/commands/_update_agent_runner.py`) calls `_do_stop` **in-process** after `git checkout` + `uv sync`, so the session-kill code that stop runs is whatever `sys.modules` holds then. Deferred imports make that stop load the just-pulled killer off disk instead of the pre-pull one (PR #932's `winproc.kill_session` fix reaches the rollout that ships it); `tests/cli/test_update_import_timing.py` fails if one is hoisted.

## Core responsibilities

### new_session contract

`env` is the **complete** child environment dict — the caller builds it (`shared.session_env.forward_env_dict` for daemons, `agent_spawn_env_dict` for agents); secrets ride the environment or a 0600 envfile, **never argv** (#974). `login_shell=True` (POSIX) wraps the command in `exec bash -lc 'cd <cwd> && <venv-prefix>exec <cmd>'` so user-local PATH additions are visible and the venv is re-activated inside the command. There are **two** shell layers to collapse — the supervisor runs a string command as `/bin/sh -c <cmd>`, and that `sh` sits in front of the login shell — so there are two `exec`s: the outer one (unconditional) hands the `sh` layer over, the inner one (`session_env.exec_into`) hands the login shell over to the command. bash-as-`/bin/sh` (macOS) execs into a lone simple command by itself, but dash (Linux) does not, so omitting the outer one leaves every Linux host recording that `sh`. The `exec` is load-bearing, not tidiness: without it the wrapper shell stays alive as the pid the supervisor records, and a graceful stop's SIGTERM lands on the shell — which, waiting on a foreground child, neither forwards it nor exits first — so the daemon never sees the signal and every graceful stop runs its full timeout into the SIGKILL fallback. A command that is compound must carry its own `exec` on the stage to supervise (`build && exec serve`); `exec_into` raises rather than silently returning to that stall. An existing live session of the same name is left untouched (idempotent).

### Backends

- **PosixProcSessionBackend** — the native supervisor for services: double-fork reparent to init, `SessionRecord` + logs under `$AVA_HOME/run/sessions/` / `$AVA_HOME/logs/`. No PTY is allocated, so the per-box PTY ceiling (`kern.tty.ptmx_max`) does not bound service count.
- **PtySessionBackend** — agent shells / watchers. Each mutating op is a `python -m shared.pty_sessions.cli` subprocess whose exit code maps to the interface shape (enumeration reads the records in-process); env rides a 0600 file, the launch command rides base64 (never argv), and the session's host submits it only once the login shell's prompt is ready. `login_shell=False` raises `NotImplementedError` (interactive login shells only); the kill timeout is owned by the host. See [[shared/pty_sessions/pty_sessions.ava.okf.md|pty sessions]].
- **WinprocSessionBackend** — Windows; kills stop at session boundaries (`winproc._spared_pids`).

### Stopping a session

`kill_session(name, graceful=...)` → `(ok, mode)`, and the platform-neutral `process_alive` / `request_stop` / `force_kill` trio for the processes that are not sessions, live in [[stopping.ava.okf.md|stopping]].

### macOS firewall audit
The read-only Application Firewall allow-list audit (issue #949, converge report + OFF_BOX_UNREACHABLE attribution): [[shared/session-backend/firewall-audit.ava.okf.md]].

## Entry points

- `shared/session_backend.py:get_backend()` / `get_shell_backend()` / `native_proc()` — the three dispatch points
- `shared/posixproc.py` / `shared/winproc.py` — the native supervisors (agent processes + services)
- `shared/pty_sessions/` — the per-session pty hosts (host + CLI + screen) for agent shells
- `shared/session_record.py:SessionRecord` — single shape for persisted background session records (`.read()`/`.write()`)
- `shared/session_env.py` — env forwarding + the envfile format shared by both POSIX backends

## Notes

- Session naming: `shared/cluster.py:session_name(service)` → `ava-<service>`. Each unit's session namespace is its own `$AVA_HOME`, so same-named sessions on different homes never collide; agent shells are prefix-isolated as `ava-agent-<id>-shell-<n>[-<name>]`.
- Shell sub-sessions are deliberately NOT torn down when the agent exits (background work outlives the process); the backends keep running even if every agent exits.
