---
type: doc
title: "PTY session kill — the whole membership, frozen then killed"
description: "Killing a PTY session takes the shell, its descendants and every process in the shell's POSIX session, pinned by birth identity, frozen with SIGSTOP and SIGKILLed children first; the session id is used only while a live captured member (or a proof under a second old) shows it still names the shell's session; a setsid'd process that left the tree is sovereign and survives."
tags:
- base
- pty
- sessions
---

# PTY session kill

## Membership

`base/sessions/pty/session_tree.py` owns what a session kill takes: the
shell, every descendant of the shell, and every process in the shell's POSIX
session (`getsid(pid) == shell pid`) together with its descendants. Job
control puts each job in its own process group, so a group signal never
reaches `cmd &`, and a double-forked member leaves the tree while keeping the
session.

Why the session id, not process groups or the tty: a double-forked member
keeps the group of a job whose leader already exited, so a group scan misses
it. The controlling tty is a session attribute, so every process on the pty
is in the session, while a member that dropped the tty or outlived its hangup
keeps the id. The id outlives the shell, so a scan uses it only while it is
proven (below).

Boundary: a process that calls setsid AND leaves the tree is outside both.
That is the shape `base._reparent` gives every sovereign launch — the
services `ava start` brings up from an agent's shell — so it survives. Nothing is ever selected by name or argv.

## Session-id proof

A pass takes a process by the shell's session id S only when S provably still
names the shell's session, checked after every read of the pass and before
any signal:

- **Kernel guarantee.** At any instant at most one session carries an id.
  Linux: `alloc_pid` takes ids from the namespace's pid idr, and `free_pid`
  (from `__change_pid`) returns one only once no task holds its `struct pid`
  as PID, thread group, process group or session. XNU: `forkproc` skips a
  candidate pid that `pfind`, `pgfind` or `session_find` still resolves. A
  new session's id is its creator's pid, so none can take S while the old
  session has a member.
- **Witness.** A captured member that reads S after the pass's reads, and is
  still the captured process (identity re-verified after that read), sat in
  one session through the whole pass: leaving takes `setsid`, which renames
  the leaver's session to its own pid. The frozen shell is the usual witness;
  with the shell dead, any other captured member is. Another process at the
  shell's pid disproves S: the kernel released that pid, so the session ended.
- **Freshness.** With no witness left, a proof under 1 s old still stands: the
  session could only be replaced under the same id if it ended and its pid
  was handed out again inside that second, and pid reuse does not land inside
  a couple of seconds. This reaches a helper that a job forks on TERM and
  orphans: it is then the session's only process, with no captured witness.
  A proven pass that read any process in the session renews the proof, so a
  fork-and-exit chain keeps it current while scans keep reading its hops.

Anything else is logged once, with pid and command name, and left running.
The scan reads session ids last, with a bare getsid sweep (~0.2 ms, against
~16 ms for the psutil pass before it), so a short-lived hop is read while it
exists and pinned (in a kill, frozen) about a millisecond later. A parent vouches for a child only
while it still is the captured process, so a member's recycled pid adds
nobody.

## Sequence

1. Pin each member before any signal: a psutil handle (it refuses a recycled
   pid) plus its `OwnedProcess` birth identity. Membership is re-read on the
   pinned handle, never trusted from the scan.
2. SIGSTOP parents before children. Once every frozen member reads stopped,
   rescan until a pass adds nobody: a stopped process cannot fork. A pass cap
   that runs out first is logged as a warning.
3. SIGKILL every member but the shell in one tight loop (liveness read before
   the first signal), children before parents and the shell's own tree last.
   An exit that orphans a process group makes the kernel SIGHUP+SIGCONT its
   stopped members; a job's leader ties the job's group to the session, so a
   double-forked member that kept the group dies before the leader.
4. The frozen shell (or a member the batch could not end) still witnesses
   the session id: one more closure takes anything that ran meanwhile. Then
   the shell dies.
5. Report every captured member still alive after the wait; `denied` names
   the ones the caller may not signal, which are not waited for (their
   liveness is read once). A kill that raises midway SIGKILLs every member it
   froze on the way out, so none stays stopped.

A graceful kill first SIGTERMs the pre-kill snapshot and waits for the shell,
then runs the same sweep (a TERM-ignoring job outlives its shell). `mode` is
`forced` whenever the sweep had to SIGKILL a live process. The service keeps
running when a session dies, so a member is never left frozen under an exited
process.

## Verdict

`interrupted` means the pre-kill snapshot held a live member beyond the shell:
a foreground job, `cmd &`, a double-forked orphan. A shell that no longer
verifies answers `interrupted` (fail-open). A dead or absent session is the
idempotent noop and answers `idle`. A survivor the caller could signal makes
the op an error naming its pid. When only processes it may not signal survive
(a root `sudo` on the pty) and the shell is gone, the op answers `ok` with
`interrupted` and a `survivors` list (`KillVerdict.survivors`), so the TTL
reaper still sends its interruption notice.

## Callers

The pty-sessions service's `kill` request (`services/pty_sessions/session.py:kill_session`)
and its terminal closure (`base/sessions/pty/closure.py`).
`ava.shell.sessions.kill`, the TTL reaper's `shell_kill`, terminate's
`kill_all_shell_sessions`, and a force stop reach the `kill` request through the
session backend.

The closure — a normal `ava stop` (`close_terminals` asks the service for
`close_all`), the service's own SIGTERM stop, and the sweep of a crashed
service's leftovers (`services/pty_sessions/ledger.py`) — captures each shell's
session with `capture_session` before any signal, HUPs the shells and TERMs the
rest. Each grace poll `refresh`es every capture with one scan, keeping its
proof current. A poll is quiet only when no captured process lives and the scan
read no non-zombie process in the session but the caller, pinned or not; it
counts only once a second, immediate poll is quiet too, since a member can fork
while the first scan runs. A capture nothing can prove any more is still scanned
and its session's processes logged. What is left after the grace dies by
`kill_session_tree(also=<capture>, proven_at=<its proof>)`
(decisions/2026-09-28-stop-escalates-to-sigkill.md,
decisions/2026-09-28-session-id-proven-by-a-live-member.md). A shell already
gone is still closed through the members the service recorded earlier: a live
recorded member proves the session id.

## Dependencies

- [[pty_sessions/pty_sessions.ava.okf.md]] — the service, its client and the session lifecycle.
