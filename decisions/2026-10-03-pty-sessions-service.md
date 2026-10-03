# Agent shells live in one pty-sessions roster service per machine

## Context

Each persistent shell ran in its own detached host process (`base/sessions/pty/host.py`), reparented
to init at creation, with a per-session unix socket, an on-disk record and its own orphan reaper
([2026-08-13-per-session-pty-hosts](2026-08-13-per-session-pty-hosts.md)). The shape existed for one
promise: no service stop, rollout or respawn can reach a shell, because a host has neither a service
identity nor a process-tree parent. The promise was already gone. Since 2026-09-28 a normal `ava stop`
closes every terminal ([2026-09-28-stop-escalates-to-sigkill](2026-09-28-stop-escalates-to-sigkill.md)),
a fleet update is `ava stop -y` and then `ava start`, and `conventions/graceful-maintenance.md` says
"Terminal state does not survive an update". What the per-session hosts still bought was survival
across an agent, agent-host or gateway restart, and the price was a miniature tmux server rebuilt
around them: a host protocol, record and identity liveness, a host-versus-shell lifecycle, an orphan
reaper, a record lock, an env handoff file and a CLI subprocess per operation (0.25 to 0.5 s each).
Of the 34 shell bugs found between 2026-08-03 and 2026-10-03, 24 sat in that layer. The sovereignty
also ran against the target process model (`future/infra/lifecycle-final-state.md`, case 1): every
long-lived process is a member of the root's tree.

## Decision

1. One ordinary roster service per machine, `pty-sessions` (`services/pty_sessions/`), holds every pty
   master and the in-memory session table. It is declared for both the gateway and the agent-runner
   capability, needs no database, is gated out only where there are no Unix sockets, and is stopped
   and started with the rest of the roster. The wire is one JSON object per line over
   `$AVA_HOME/run/pty-sessions.sock` (mode 0600): requests `{id, method, ...}`, responses echoing the
   `id`, so the shared ownership probe (`services.healthchecks.owned_service`) reads it like the other
   Unix-socket services. `PtySessionBackend` is a client of that socket (`base/sessions/pty/client.py`),
   one short connection per request, so a restarted agent simply dials again.
2. The guarantee shrinks to what the code can keep: a session outlives an agent process, an agent
   host and a gateway restarting, because each of those is a restart of a *client*; the service is
   untouched by it. A service-level stop is not that: `ava stop`, `ava restart` (the update ladder
   restarts with it, and the service must run the new code afterwards), a fleet update, a crash of
   the service and a reboot all end every session. Nothing is promised that the update path breaks.
3. Stopping is the service's job. `ava stop` and `ava restart` keep the unit through the services
   phase, ask the service to `close_all` (the one terminal closure,
   `base/sessions/pty/closure.py`: HUP the shells, TERM the rest of each captured POSIX session, a
   bounded grace, SIGKILL whole sessions), write the owners' closure notices from the answer, then
   stop the unit and, with nothing else kept, the root. `--force` closes without notices. The one
   way to keep sessions across a stop is the generic `--keep-service pty-sessions`, which leaves the
   unit and its sessions alone; no command uses it by default. `ava maintenance stop` refuses while
   any session is live and has no option to proceed. A service that receives SIGTERM closes what is
   still alive before it exits.
4. Crashes are swept by identity. The service alone writes a ledger (`run/pty-sessions.json`): each
   live shell's identity and the members of its session last seen alive, rewritten as sessions come
   and go and every ten seconds. A crash closes the masters, which hangs up every shell; what ignored
   the hangup is closed at the next start (or by a stop that finds no service) from the ledger, each
   process verified by birth before a signal. That sweep returns the busy sessions it closed in the
   closure's own shape, so a later change can write their owners' notices; today it logs them.
5. A shell's base environment is the service's, minus `AVA_PROCESS_PROFILE` and `VIRTUAL_ENV`, overlaid
   by the caller's forwarded env. A variable that only the creating process held (an agent's
   `SSH_AUTH_SOCK`) no longer rides into the shell unless it is forwarded.
6. The per-session hosts and everything that existed to coordinate them are deleted, with no alias:
   the host process and its launch, the record files and their lock and sweep, the orphan reaper,
   `kill_host_tree`, the envfile handoff and the CLI subprocess transport. What is kept is the
   evidence machinery that has nothing to do with hosts: `session_tree` (membership and kill proof),
   the allocation freeze, the pyte screen and the closure notices.

## Alternatives rejected

- **Keep the per-session hosts.** They only protect against a restart of the agent side, which a
  service also survives, and they cost the 24 layer bugs, a process per shell, a CLI start per
  operation and a second model of ownership next to the root tree.
- **tmux (or screen, dtach) as the host.** The kill proof (about 1500 lines in `session_tree`) is a
  product requirement, not a host artifact: a pane's hangup does not take `cmd &`, a double fork or a
  helper forked in a TERM handler. A tmux server also brings its own environment inheritance, argv
  leakage of env, target-name matching and per-cluster socket isolation back, which the 2026-08 move
  away from tmux had just paid off. Walking the 34 bugs against it, about half of the small ones
  disappear and none of the expensive ones.
- **A supervisor daemon that hands masters over between generations (SCM_RIGHTS).** It works only when
  the outgoing process is alive to hand off, which is not the crash case, and the roster service gets
  the same effect for the cases that matter (agent-side restarts) with no handoff protocol.
- **A forked spawner process for `pty.fork`.** The service forks from request threads of a
  multi-threaded process; the child does only a window-size ioctl, chdir, signal resets and exec with
  an environment built beforehand. Twenty to eighty sessions printing continuously while threads
  allocate three hundred more produced no stuck child and a worst ping of about 200 ms, so the extra
  process is not needed.
- **A thread per connection and per session.** The structure baseline allows no new free-floating
  thread, and one loop that reads every master is what a many-descriptor process wants anyway (no
  `select` descriptor ceiling). The loop does only I/O; every request that can block runs on the
  executor, and `ping`, which the ownership probe times out at three seconds, is answered on the loop.
- **Closure notices from the service.** It would need a database client in a service that is
  deliberately free of one. The stop writes them from the `close_all` answer, as before.

## Consequences

- A service crash, a forced stop of its unit, or a sweep at start ends sessions without a notice to
  their owners. This is the silent gap the per-session hosts had on a host crash, widened from one
  session to the machine; closing it (a one-shot process that writes the notices for the sweep's busy
  sessions) is a separate change.
- `ava restart` no longer spares shells: a smooth restart used to leave them, and the update ladder
  restarts with it, so a restart now closes every terminal and notifies its owner like a stop does.
  `ava maintenance stop --keep-terminals` is gone with the per-session hosts it protected.
- One process now holds every master. A fault in it ends every session instead of one; a wedged
  service reads as DOWN to the root's health round and is replaced, which ends them too. The service
  therefore keeps its event loop free of blocking work and its ping off the executor.
- Descriptor limits are the service's: it raises its soft `RLIMIT_NOFILE` toward 10240 at start, and
  the per-box pty ceiling (`kern.tty.ptmx_max`, 511 by default on macOS) is unchanged.
- Under macOS the shell's parent chain is the signed helper, `ava-root`, `pty-sessions`, the shell; its
  TCC attribution follows that chain and is verified on a real host, not in tests.
- A sandboxed exec call reaches the socket directly, as it reaches the other daemons' sockets; that
  path is covered by the client, not by a sandbox test.
- No migration and no port slot: the unit appears in the roster at the update that ships it. Hosts
  left by the previous code are closed by that code's own `ava stop` in the update's down leg.
