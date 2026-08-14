# Per-session pty hosts: shell persistence became structural, the supervisor daemon was deleted

**Date**: 2026-08-13
**Status**: decided
**Context**: the 2026-08-12 prod incident — a cluster update's service stop
force-killed `ava-pty-supervisor`, taking every agent shell down with it
(including the shell the rollout itself ran in, which killed the rollout
mid-stop and stranded prod paused with all services down).

## Problem

The repo promised, in six places, that agent persistent shell sessions
"persist across terminate/restart/update" — while the update path stopped the
pty-supervisor daemon like any roster service, and every shell was a child of
that daemon holding its pty master. The promise held against agent restarts
and gateway restarts, and was false against cluster updates, `ava stop`, and
the supervisor healthcheck's kill-then-relaunch respawn. The cost had been
paid repeatedly (#1014 ×4 — watchers reaped by every rollout; #973 — all page
servers dead after a rollout), and each remediation built a *rebuild*
mechanism (watcher registry, page-server heartbeat probe) rather than making
the sessions survive — rebuild cannot save a half-finished training run.

## Decision

Delete the supervisor daemon entirely. Each session runs in its **own
detached host process** (`shared/pty_sessions/host.py`): spawned via
`shared._reparent` so it reparents to init at birth, owning its one pty
master, screen model, transcript, record, and a per-session unix socket
(`$AVA_HOME/run/pty/<name>.sock`). `new` spawns a host; session ops dial the
session's socket; enumeration is a record scan. There is no roster entry, no
healthcheck, no respawn channel, and no `_FORCE_KILL_SESSIONS` carve-out —
the mass-kill channels are not guarded against, they no longer have a target.
A session ends only via its own `kill`, its shell exiting, its own host
crashing (blast radius: one session), or a machine reboot.

## Alternatives rejected

- **Exempt the supervisor from the stop scope** (`preserve_sessions` +
  watchdog special-casing — the browser's mechanism). Rejected: it protects
  one channel at a time (update stop, `ava stop` reap, healthcheck respawn,
  watchdog code-controller restart — each needing its own carve-out that new
  channels silently miss), leaves the supervisor running old code across
  updates, and keeps the machine-wide blast radius: any supervisor death
  still kills every shell on the box.
- **Master-fd handoff (SCM_RIGHTS) between supervisor generations.**
  Rejected: only works when the outgoing supervisor is alive to hand off —
  useless against SIGKILL (the force-kill path and the crash case, which are
  exactly the cases that hurt), and it keeps the single point holding every
  master.
- **Keep the daemon as a router proxying to per-session hosts.** Rejected:
  once hosts own the sessions, the daemon's remaining duties (spawn, list)
  do not need a resident process — creation is an in-process spawn, listing
  is a record scan. A router would be a mortal process in the I/O path with
  a healthcheck, a roster row, and an update interaction, purchased for
  nothing.

## Consequences

- The supervisor-era "session dies with the daemon" bound
  (2026-05-09-stateless-gateway.md, "accepted as equivalent to closing a
  terminal window") now applies per session instead of per machine.
- One transition cost: the update that ships this reaps the old
  `ava-pty-supervisor` service session (converge `_RENAMED_AWAY_SERVICES`),
  killing the final pre-host-era shells — the last time an update kills any
  shell.
- Session creation pays a python interpreter start (~0.4s measured) instead
  of a daemon round-trip — noise against the `bash -l -i` login itself.
- Each shell costs one extra small process (the host); the per-box pty
  ceiling (`kern.tty.ptmx_max`) is unchanged and remains the density wall.
- `has` (record + shell-pid liveness) is now truthful unconditionally — the
  "stays truthful while the daemon restarts" caveat is gone with the daemon.
