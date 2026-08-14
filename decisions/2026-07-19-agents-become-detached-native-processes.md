# Agents become detached native processes — double-fork spawn

2026-07-19. Moved the **agent process** out of the session server. An agent is
now spawned as a detached, native OS process by a native process supervisor
(`shared/posixproc.py` on POSIX, the pre-existing `shared/winproc.py` on Windows),
tracked by `agents_meta.pid` + a JSON session record under
`$AVA_HOME/run/sessions/`. Daemons and the agents' own persistent shells
(`ava.shell`) stay on the session server.

## Why

An agent process is non-interactive: it talks to the world over Postgres + Redis
and logs to a file. The server session that hosted it only ever cost a **PTY**, and
macOS caps allocatable PTYs host-wide at `kern.tty.ptmx_max` (default 511) — a
whole-box wall, not per-process. On the dense single-box target (100-300 agents)
this was the *first* hard ceiling: with a real baseline of ~120-140 PTYs before
any agents, session spawn started failing with `fork failed: Device not
configured` around ~370-380 agents, a wall RAM and fd headroom did not reveal.
Retiring the per-agent PTY removes that ceiling as a function of agent count.

The session server gave the agent three things — a named handle, a liveness check, a kill by
name. All three were already redundant: the agent writes its own pid to
`agents_meta` at claim, the restarter reaps by `process_alive(pid)`, and
force-terminate kills by that pid. The session was pure overhead.

## The mechanism (and why double-fork)

The spawner is long-lived (the gateway / ops daemon). A naive
`subprocess.Popen(start_new_session=True)` leaves the agent as a child of that
spawner; when the agent exits, nothing `waitpid()`s it and it accretes as a
**zombie** (the same class of bug that once drove daemons *back into* the session server — see
`shared/service_respawn.py`). So the agent must reparent onto init immediately.

`shared/posixproc.new_session` runs a tiny reparenting helper
(`shared/_reparent.py`) via `subprocess.run` and waits on it: the helper
`setsid()`s, forks once, prints the grandchild's pid, and exits — so the agent
(grandchild) reparents to **init**, and the spawner's only direct child (the
helper) is reaped by the `subprocess` wait. No zombie, no PTY.

Alternatives rejected:
- **Bare `os.fork()` double-fork in the spawner.** The gateway is a multithreaded
  asyncio process; `os.fork()` from a worker thread risks a deadlock if another
  thread holds a lock the child's at-fork handlers need. Doing the fork in a fresh
  single-threaded helper (reached via `subprocess`'s async-signal-safe fork+exec)
  sidesteps this entirely.
- **`setsid --fork <prog>`** (a standard-binary double-fork): `setsid(1)` is not
  shipped on macOS, and prod runs on a macOS host.
- **`signal(SIGCHLD, SIG_IGN)` / a reaping thread in the spawner**: `SIG_IGN`
  breaks `subprocess.wait()` for every *other* subprocess the gateway/ops run
  (git, …); a reaping thread is fragile across two spawner processes.
- **Agent self-daemonizes in `agent/__main__`**: spreads pid-record ownership into
  the agent and muddies the clean "supervisor owns the record" split.

## Consumer migration

Everything that used to reach the agent through the session server now goes through the native
supervisor or the DB pid:
- **spawn / kill-stale** (`ops/agent_launch.py`) → `native_proc()` (winproc /
  posixproc), both platforms.
- **`ava stop` reap** (`cli/commands/stop.py`) → SIGTERM every agent process under
  one shared deadline, force-kill stragglers; the agents' **shells** are still
  reaped via the shell backend. Self-contained (no DB — it is being torn down), enumerating
  agents from the on-disk session records.
- **force-terminate** (`ops/ops_lifecycle.py`) → `native_proc().kill_session` +
  `force_kill(pid)`.
- **cluster status `agent_count`** (`ops/cluster.py`) → counts native-supervisor
  records (the pid source); `shell_count` / `session_count` / `agent_groups`
  stay on the shell backend (shells).
- **restarter reapers** (`ops/controllers/respawn.py`) were already pid-based — no
  change.

The `_warn_if_pty_ceiling_low` startup warning (which tied `ptmx_max` to
`max_agents` on the one-PTY-per-agent assumption) was removed: that coupling no
longer exists.

The record layer duplicates the DB pid only for the *no-DB* path (`ava stop`
after the gateway + DB are down); the DB pid stays authoritative everywhere a
process can reach it.
