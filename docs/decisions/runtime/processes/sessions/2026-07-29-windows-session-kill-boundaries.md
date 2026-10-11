# Windows session isolation is enforced at the kill, not at the launch

## Context

POSIX gets one property for free that the Windows supervisor never had: a
session's lifetime is independent of whatever spawned it. The session server gives it from the
launch side (a pane belongs to the session server, not to the caller of a spawn
new-session`), and `shared/posixproc.py` buys the same thing for agent processes
with a double fork through `shared/_reparent.py` — the child reparents to init
before the spawner returns.

`shared/winproc.py` has neither. Windows has no `fork`, no `setsid`, and no init
that adopts orphans; `DETACHED_PROCESS` suppresses the console and
`CREATE_NEW_PROCESS_GROUP` gives Ctrl-Break a target, but neither touches the
parent link. A session a daemon spawns therefore stays that daemon's child in the
process table forever, and `kill_session` walked `children(recursive=True)` —
children first, so a *spawned* session died before its spawner.

That closed the Windows agent-runner's self-update. A gateway-triggered update
POSTs `/ops`, the ops daemon calls `spawn_update()` in-process, and the
`ava-updater` session is born as `ava-ops`'s child. The updater's own `ava
restart` then stops this host's services — `ava-ops` among them — and killed
itself doing it. Observed on `win` 2026-07-29: `ava-updater.out.log` ends one
line after `✓ ava-agent-runner-watchdog`, with `ava-ops` next in the stop list;
the box was left stopped, un-updated, and dependent on the watchdog to come back.
Agent processes were exposed identically — `ava update`'s stop deliberately
leaves them running for the rollout to quiesce, yet stopping `ava-ops`
force-killed every agent it had launched.

## Decision

`winproc.kill_session` prunes the tree walk instead of widening it. Two rules
(`_spared_pids`): skip the subtree of every *other* live session record, and skip
the caller's own process and its ancestors below the target. A session's lifetime
belongs only to the caller that names it — the same invariant the session server and
`posixproc` provide, reproduced from the kill side.

## Alternatives rejected

- **Reparent at launch (a Windows analog of `shared/_reparent`).** Spawn a
  short-lived helper that launches the real child detached and exits, so the
  recorded pid's parent is already dead. It is the closer mirror of POSIX and it
  would also survive a tree kill Ava did not issue. Rejected because it changes
  the launch shape of *every* Windows session — daemons, agents, orchestration —
  to fix a defect that lives entirely in the kill, and because the property it
  buys cannot be asserted off-box: a test can only mock the very `Popen` call
  whose real Windows behaviour is the open question. This module's history is two
  launch-side surprises already (cmd.exe stealing its children's stdout handles
  under `DETACHED_PROCESS`; `os.execvp` spawning-and-exiting so the recorded pid
  was not the surviving process). The pruning, by contrast, is a pure decision
  over a process tree and is asserted directly on macOS CI. Nothing here blocks
  adding the helper later; the two compose.
- **Reparent the updater onto Task Scheduler (`schtasks /create … /run`), so the
  ops daemon is not its parent at all.** Task names are host-global while
  clusters are home-scoped, so two co-located clusters would collide on one
  `ava-updater` task; it also puts a registered OS job on the critical path of
  every update and needs its own cleanup story for a task that exists only for
  the duration of one run.
- **Reorder `_do_stop` to kill `ava-ops` last.** Only narrows the window: the
  updater still dies, just at the end of the stop instead of the middle, and the
  agent-kill half of the defect is untouched.

## Consequences

- Ava's own stops no longer cross session boundaries on Windows. A tree kill
  issued by something else (`taskkill /T`, Task Scheduler ending a task) still
  takes descendants with it — that residue is what launch-side reparenting would
  have removed.
- The kill can now leave a process alive that the target spawned, if that process
  happens to hold a live session record. This is deliberate: `kill_session` stops
  one session, and stopping the rest is `_do_stop`'s enumeration to decide, not a
  tree walk's.
- Correctness no longer depends on session records surviving the teardown: the
  second rule (self + ancestry) protects the process running the stop even after
  its own record has been unlinked.
