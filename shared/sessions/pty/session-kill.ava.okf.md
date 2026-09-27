---
type: doc
title: "PTY session kill — the whole membership, frozen then killed"
description: "Killing a PTY session takes the shell, its descendants and every process in the shell's POSIX session, pinned by birth identity, frozen with SIGSTOP and SIGKILLed children first; a setsid'd process that left the tree is sovereign and survives."
tags:
- shared
- pty
- sessions
---

# PTY session kill

## Membership

`shared/sessions/pty/session_tree.py` owns what a session kill takes: the
shell, every descendant of the shell, and every process in the shell's POSIX
session (`getsid(pid) == shell pid`) together with its descendants. Job
control puts each job in its own process group, so a group signal never
reaches `cmd &`, and a double-forked member leaves the tree while keeping the
session.

Why the session id, not process groups or the tty: a double-forked member
keeps the group of a job whose leader already exited, so a group scan misses
it. The controlling tty is a session attribute, so every process on the pty
is in the session, while a member that dropped the tty or outlived its hangup
keeps the id. The kernel never reuses a pid that still names a session, so the
id cannot land on a stranger (`shared.proc.hosting_exec_domain` asks the same
question the same way).

Boundary: a process that calls setsid AND leaves the tree is outside both.
That is the shape `shared._reparent` gives every sovereign launch — a new PTY
host, the services `ava start` brings up from an agent's shell — so it
survives. Nothing is ever selected by name or argv.

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
4. The shell is still frozen and alive, so the session id names only this
   session: one more closure takes anything that ran meanwhile. Then the
   shell dies.
5. Report every captured member still alive after the wait; `denied` names
   the ones the caller may not signal. A kill that raises midway SIGKILLs
   every member it froze on the way out, so none stays stopped.

A graceful kill first SIGTERMs the pre-kill snapshot and waits for the shell,
then runs the same sweep (a TERM-ignoring job outlives its shell). `mode` is
`forced` whenever the sweep had to SIGKILL a live process. The host counts a
kill op in flight until it answers, and its exit after the shell dies waits
for it, so a member is never left frozen under an exited host.

## Verdict

`interrupted` means the pre-kill snapshot held a live member beyond the shell:
a foreground job, `cmd &`, a double-forked orphan. A shell that no longer
verifies answers `interrupted` (fail-open). A dead or absent session is the
idempotent noop and answers `idle`. A survivor the caller could signal makes
the op an error naming its pid. When only processes it may not signal survive
(a root `sudo` on the pty) and the shell is gone, the op answers `ok` with
`interrupted` and a `survivors` list; the CLI prints `interrupted` and names
them on stderr, so the TTL reaper still sends its interruption notice.

## Callers

The host's `kill` op; the CLI's record-based kill of a wedged host
(`_kill_by_record`); the lazy sweep of a crashed host's surviving shell
(`records._kill_recorded_shell`); and, through `kill_host_tree` (which
freezes the host first and kills it even when a session kill raised), the
orphan-host reaper and a failed spawn's abort.
`ava.shell.sessions.kill`, the TTL reaper's `shell_kill`, terminate's
`kill_all_shell_sessions`, and a force stop reach the host op through the
session backend.

A normal `ava stop` (`cli/commands/_temporary_stop.py`) captures each shell's
membership with `session_members` before any signal, HUPs the shells, TERMs
the rest, and after a bounded grace kills what is left with
`kill_session_tree(also=<the capture>)`
(decisions/2026-09-28-stop-escalates-to-sigkill.md). `ava pause` closes no
terminal.

## Dependencies

- [[pty_sessions.ava.okf.md]] — session lifecycle and record ownership.
