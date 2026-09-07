---
type: doc
title: "PTY sessions — one detached host process per agent interactive shell"
description: "Each agent shell/watcher owns a pty host, screen model, capture buffer, and unix-socket protocol. Opted-in macOS hosts are direct permissions-helper children; others reparent to init."
tags:
- shared
- pty
- sessions
---

# PTY sessions

## What it is

[[liveness.ava.okf.md|Liveness]] excludes matching but unreaped zombie PIDs.

`shared/pty_sessions/` hosts every agent interactive shell — **one detached
host process per session**, no supervisor daemon. The SDK keeps its
named-session surface (`ava.shell.sessions`, watchers, schedules) unchanged.

Four modules:

- `allocation_freeze.py` — the host-wide marker and allocation mutex. The
  marker carries one operator-owned generation; only that generation can
  resume allocation. It has no gateway or data-plane dependency.

- `host.py` — the per-session host: the `pty.fork()` shell, a reader thread
  feeding a pyte screen model + raw byte ring buffer + byte transcript, and
  the session's own unix socket answering `send` / `send_keys` / `capture` /
  `resize` / `kill` / `ping` as JSON lines. Exits when its session dies.
- `cli.py` — the transport contract the SDK consumes (`has` / `new` / `send` /
  `send_keys` / `capture` / `resize` / `kill` / `list` / `started-at` /
  `list-started-at`), the send-keys key table, and the 0600 envfile writer.
  `new` spawns a host; enumeration reads the records — no process to dial.
- `screen.py` — the pyte wrapper: incremental UTF-8 decode, raw ring buffer,
  screen-parity capture rendering.

## Why per-session hosts (the load-bearing shape)

A host is absent from the service roster and is probed by no watchdog. It
normally reparents to init through `shared._reparent`. On macOS, enabling both
helper switches makes it a direct signed-helper child with stable TCC
responsibility; its pty record—not the volatile helper table—remains lifecycle
truth. Every mass-teardown channel that used to kill shells —
`ava cluster update`'s service stop, `ava stop`'s tree kill, a healthcheck
`respawn_and_verify` — reaches processes by service identity or process
tree, and a session host has neither. That is what makes the SDK's promise
"sessions persist across terminate/restart/update" **structural**: a session
ends only through its own `kill` op, its shell exiting, that one host
crashing (blast radius: one session), or a machine reboot.
Decision: [2026-08-13-per-session-pty-hosts](../../decisions/2026-08-13-per-session-pty-hosts.md).

The pty master fd lives in the host, so host death IS session death (the
slave hangs up) — the per-session equivalent of closing one terminal window,
and exactly the accepted semantics the 2026-05 session server had for ALL
sessions at once. Sovereignty was moved down to the unit that owns one
terminal.

## Namespace + records

Everything per-session lives under `$AVA_HOME/run/pty/`:

- `<name>.json` — the record: a `SessionRecord` (shell pid + create_time =
  the liveness key; "session alive" MEANS the shell is alive) plus
  `host_pid` / `host_create_time` so a kill can reach a wedged host.
  `SessionRecord.read` ignores the extras.
- `<name>.sock` — the session's socket (sun_path-bounded names fall back to
  a hashed tempdir path, computed identically by host and CLI).

Allocation control is deliberately outside every per-home namespace. Beside
the host-level cluster registry are `pty-allocation-freeze.json` and the stable
`pty-allocation.lock`; changing `$AVA_HOME` therefore cannot bypass a freeze.
The marker stores schema version, random generation, holder, reason, and UTC
creation time. A malformed marker means frozen, never inactive.

Deliberately NOT `run/sessions/` (the posixproc/winproc dir): the record
scan IS the session listing (`list`, `list-started-at`, and the backend's
in-process enumeration — task #1200's snapshot cost, now zero round-trips),
and sharing the dir would force every lister to regex-filter the other
side's records. A record whose shell is dead is a crashed host's leftover
and is swept lazily as enumeration discovers it. Transcripts stay at
`$AVA_HOME/logs/<name>.out.log`; host diagnostics at `<name>.host.log`.
Host startup never deletes either file. The converge-owned daily logs job
copytruncates active `.out.log` files through `ava logs rotate`, then `ava logs
retention` prunes allowlisted files while skipping active open handles; its
named-PTY rule covers `<name>.{out,host}.log` without traversing any subtree.

## Lifecycle

- **create** — CLI `new`: under the host allocation lock, reap name-specific
  orphans, return success for an already-live same-name session, refuse an
  absent session when the marker is frozen or invalid, otherwise helper direct
  spawn on enabled macOS or `_reparent` on every other route → `host.py <name>
  <cwd> <envfile> <generation> [cmd_b64]`. The lock stays held until the
  host answers ready, so a completed `ava pty freeze` is an exact boundary:
  every earlier allocation is visible and every later allocation is refused.
  A host that misses its ready deadline is terminated with its process tree
  before the lock is released, so it cannot publish a late record across that
  boundary.
  The host consumes the 0600
  envfile (values never on argv, #974), binds its socket, forks the shell
  (child drops `AVA_PROCESS_PROFILE`, overlays the envfile), writes the
  record, and answers `ping`; the CLI waits for readiness and fails fast
  with the host log tail if the host dies pre-socket.
- **death** — the reader reaps the child (waitpid on every pass) and on EOF
  or reap closes the master (hanging up the slave's foreground group),
  unlinks record + socket, and exits the process after a short drain.
- **kill** — the host signals the shell's group AND the tty's foreground
  group (`tcgetpgrp`), graceful SIGTERM first when asked, then SIGKILL +
  psutil-walk backstop. A host that stops answering is killed straight from
  the record (`_kill_by_record`), so `kill` stays authoritative.
- **signals** — the host SIG_IGNs SIGHUP/SIGTERM/SIGPIPE: a stray hangup or
  a TERM aimed at the shell's tree must not take the session down; ending a
  session is the kill op's job. SIGKILL ends host + session.
- **operator freeze** — `ava pty freeze --holder HOLDER --reason REASON`
  atomically creates one random generation. The allocation command itself
  does not directly kill a PTY, but desired-state reconcilers treat the new
  generation as the boundary immediately;
  `ava pty status` reads it locally; `ava pty resume GENERATION` releases only
  that exact allocation freeze and retains its UUID as the active session
  generation. A stale token cannot activate a replacement freeze.

## Consumers

`shared/session_backend.PtySessionBackend` (`get_shell_backend()` on POSIX)
— mutating ops via CLI subprocess, enumeration via the in-process record
scan. Above it: `ava.shell.sessions`, `ava.watcher`, the gateway
ScheduleManager, the page-server daemon, `ops.ops_cluster.capture_shell`, and
`ava stop`'s shell reap. Every creation path crosses the same host lock.

## Boundaries

- POSIX-only (`pty.fork`; Windows has no pty backend —
  [conventions/windows-setup.md](../../conventions/windows-setup.md)).
- One pty per session counts against the host-wide `kern.tty.ptmx_max`
  ceiling (macOS default 511) — see `shared/platform.py`.
- [[generation-boundary.ava.okf.md]] defines the desired-state implications of
  a freeze and the fail-closed corrupt-marker repair contract.
- Session records carry the generation under which their host was admitted.
  A desired-state owner may rebuild a missing record only when its persisted
  desired generation is current; superseded exact records are reaped instead.
- A machine reboot ends every session (hosts are processes, not state);
  the watcher registry ([[shared/watcher_registry.ava.okf.md]]) is the
  rebuild net for watchers, and page servers self-heal via the heartbeat
  probe.
