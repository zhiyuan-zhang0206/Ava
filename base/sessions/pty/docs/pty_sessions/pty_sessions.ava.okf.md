---
type: doc
title: "PTY sessions — every agent interactive shell lives in the pty-sessions service"
description: "One ordinary roster service per machine holds every agent shell's pty master, screen model and transcript and serves a unix-socket JSON-line protocol; the SDK's backend is its client. Sessions outlive agent-side restarts and end with a service-level stop."
tags:
- base
- pty
- sessions
---

# PTY sessions

## What it is

[[../liveness.ava.okf.md|Liveness]] excludes matching but unreaped zombie PIDs.

Every agent interactive shell and watcher runs in the machine's `pty-sessions`
service (`services/agent_runner/pty_sessions/`), an ordinary roster process (both the
gateway and the agent-runner capability, no database, gated out only where
there are no Unix sockets). It holds each session's pty master and an in-memory
session table; `base/sessions/backend.PtySessionBackend` is its client, and the
SDK keeps its named-session surface (`ava.shell.sessions`, watchers,
schedules) unchanged. Decision:
[2026-10-03-pty-sessions-service](../../../../../docs/decisions/runtime/processes/sessions/2026-10-03-pty-sessions-service.md).

Client side, in this package:

- `client.py` — one short unix-socket connection per request, so a restarted
  agent simply dials again. Queries (`has_session`, `list_sessions`,
  `live_sessions`) read a service that is not listening as "no sessions"
  (`ServiceDownError`); mutating requests raise, after a bounded wait for a
  service still coming up. `kill` of a session with no service is a noop.
- `protocol.py` — the wire: one JSON object per line, `{id, method, ...}` in,
  `{id, ok, code, data, error}` out, the `id` echoed so the shared ownership
  probe pairs a ping with its answer. Codes: 0 ok, 1 error, 2 bad request, 3 no
  such session.
- `keys.py` — the send-keys vocabulary, translated to bytes before dialing.
- `paths.py` — the socket (`run/pty-sessions.sock`, or a fixed
  `/tmp/ava-pty-<uid>/<digest>.sock` when the home is too long for `sun_path`),
  the instance lock, the ledger and the transcript locations.
- `closure.py` — the one terminal closure, see below.
- `process_groups.py` — bounded known-group signaling, see
  [[../session-kill.ava.okf.md|session kill]].
- `allocation_freeze.py` — the home's marker and allocation mutex. The marker
  carries one operator-owned generation; only that generation can resume
  allocation. It has no gateway or data-plane dependency.
- `screen.py` — the pyte wrapper: incremental UTF-8 decode, raw ring buffer,
  screen-parity capture rendering.

Service side, in `services/agent_runner/pty_sessions/`: `service.py` (the session table, the
request handlers, the event loop), `session.py` (one session: the `pty.fork()`
login shell, its screen, ring and transcript, its kill), `ledger.py` (the crash
ledger and its sweep), `daemon.py` (the entry point and instance lock),
`shutdown_budget.py` (the stop window root derives from).

## Requests

`ping`, `has`, `list` (every live session with pid, birth identity, cwd,
start time and allocation generation), `new`, `send`, `capture`, `resize`,
`kill` and `close_all`. `new` carries the cwd, the caller's env and the optional
initial command in the request body of a 0600 socket (values never reach an
argv, #974). The service's event loop reads every master and does only I/O;
every request that can block (a fork, a bounded kill, a
capture render) runs on the executor, and `ping` is answered on the loop so the
ownership probe, which times out at three seconds, never queues behind them.

A shell's base environment is the service's, minus `AVA_PROCESS_PROFILE` and
`VIRTUAL_ENV`, overlaid by the caller's forwarded env. A variable that only the
creating process held does not ride into the shell unless it is forwarded.

## Session lifetime

A session ends with its shell, a `kill`, a `close_all`, or the service stopping.
It does not end when an agent, an agent host or a gateway restarts: each is a
restart of a client. A service-level stop is not: `ava stop`, `ava restart`, a
fleet update, a crash of the service and a reboot end every session; the only way
to keep sessions across a stop is the generic `--keep-service pty-sessions`.
Creation, death, kill, signals and the operator freeze are in
[[session-lifetime.ava.okf.md|session lifetime]].

## Closure and the ledger

`close_all` runs the one terminal closure inside the service; a ledger lets the
next start sweep what a crashed service left running. Both are described in
[[closure-and-ledger.ava.okf.md|closure and ledger]]. `ava stop` turns the
closure's answer into owner notices (`ops/pty_close_notices.py`) and fails on a
surviving shell; known job leftovers are diagnostic; new allocations are refused for its duration.
A crash's busy sessions are staged on disk and told by a one-shot child at the
next start; a batch the child does not finish is re-sent by the start after.

## Namespace

- `$AVA_HOME/run/pty-sessions.sock` — the socket (0600).
- `$AVA_HOME/run/pty-sessions.lock` — held while a service runs.
- `$AVA_HOME/run/pty-sessions.json` — the ledger.
- `$AVA_HOME/run/pty-close-notices.json` — crash notices the child still owes
  their owners (removed once written; a leftover is re-sent at the next start).
- `$AVA_HOME/logs/<name>.out.log` — the session's byte transcript, capped, which
  the converge-owned daily logs job copytruncates through `ava logs rotate` and
  `ava logs retention` prunes (its named-PTY rule covers `<name>.out.log`).
- `$AVA_HOME/pty-allocation-freeze.json` and `pty-allocation.lock` — the
  allocation freeze.

## Consumers

`base/sessions/backend.PtySessionBackend` (`get_shell_backend()` on POSIX).
Above it: `ava.shell.sessions`, `ava.watcher`, the gateway ScheduleManager, the
page-server daemon (one `list` per reconcile pass), `ops.cluster_status`
capture/kill, the worktree-removal guard (`base/deploy/git/worktree_guard.py`,
read-only: it never creates a home) and `ava stop`'s terminal closure
(`cli/commands/lifecycle/service_stop.py`). Every creation path crosses the same
allocation lock.

## Boundaries

- POSIX-only (`pty.fork`; see
  [Windows host guidance](../../../../../docs/conventions/operations/windows-setup.md)).
- One pty per session counts against the host-wide `kern.tty.ptmx_max`
  ceiling (macOS default 511) — see `base/native_process/os_platform.py`. The
  service raises its soft descriptor limit toward 10240 at start.
- One process holds every master: a fault in it ends every session instead of
  one, and a wedged service reads as DOWN to the root's health round.
- [[../generation-boundary.ava.okf.md]] defines the desired-state implications of
  a freeze and the fail-closed corrupt-marker repair contract.
- A session carries the generation under which it was admitted. A desired-state
  owner may rebuild a missing session only when its persisted desired generation
  is current; superseded exact sessions are reaped instead. A reboot ends every
  session; the ScheduleManager rebuilds its own, page servers recover via
  heartbeat, and a watcher — no desired-state record — rebuilds nothing
  (docs/decisions/runtime/updates/recovery/2026-09-27-watchers-are-never-restarted.md).
