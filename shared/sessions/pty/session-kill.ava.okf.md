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
   rescan until a pass adds nobody: a stopped process cannot fork.
3. SIGKILL children before parents, the shell last and only after the rest
   exited, so no stopped job sees its group orphaned (the kernel would
   SIGHUP+SIGCONT it awake).
4. Report every captured member still alive after the wait. The host's `kill`
   op then answers an error naming the pids instead of success.

A graceful kill first SIGTERMs the pre-kill snapshot and waits for the shell,
then runs the same sweep (a TERM-ignoring job outlives its shell). `mode` is
`forced` whenever the sweep had to SIGKILL a live process.

## Verdict

`interrupted` means the pre-kill snapshot held a live member beyond the shell:
a foreground job, `cmd &`, a double-forked orphan. A shell that no longer
verifies answers `interrupted` (fail-open). A dead or absent session is the
idempotent noop and answers `idle`.

## Callers

The host's `kill` op; the CLI's record-based kill of a wedged host
(`_kill_by_record`); the lazy sweep of a crashed host's surviving shell
(`records._kill_recorded_shell`); and, through `kill_host_tree` (which
freezes the host first), the orphan-host reaper and a failed spawn's abort.
A release's or PITR activation's terminal closure (`close_release_terminals`,
`cli/commands/service_stop.py`) kills what its cancel left through
`kill_session_tree`, rooted at each recorded shell and at the members captured
before the cancel. `ava.shell.sessions.kill`, the TTL reaper's `shell_kill`,
terminate's `kill_all_shell_sessions`, and a force stop reach the host op
through the session backend.

Not a kill: the normal `ava stop` terminal closure (`close_terminals`, same
module) captures the same membership (`session_members`), HUPs the shells and
TERMs every other member without escalation; a shell exiting on its own
closes the pty, and jobs that ignore the hangup outlive it (pane semantics).

## Dependencies

- [[pty_sessions.ava.okf.md]] — session lifecycle and record ownership.
