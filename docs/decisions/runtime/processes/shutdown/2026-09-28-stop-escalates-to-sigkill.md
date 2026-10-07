# A normal stop SIGKILLs the terminals that outlive a bounded grace

## Context

`ava stop` closes this unit's persistent shells after the agents drained and
the services stopped (`cli/commands/_temporary_stop.py:_stop_terminals`). It
captured each shell's descendants, sent the shell SIGHUP and the descendants
SIGTERM, and waited for all of them until the stop's deadline (300 s by
default). By design it never escalated. A process that ignored the hangup kept
the maintenance hold and failed the stop. That was the same rule the service
stop follows (`conventions/graceful-maintenance.md`: "a deadline is a failed
stop, not permission to kill survivors").

Two things were wrong with that for terminals:

- **The capture was only the shell's tree.** A job that double-forked, or ran
  `nohup cmd &` from a subshell, is reparented to init. It leaves the tree but
  stays in the shell's POSIX session. The stop never saw it: the stop
  reported success, and the orphan lived on. A watcher-shaped orphan could
  then wake an agent that the stop had just drained. PR #3521 made the PTY
  session kill take the whole membership (`shared/sessions/pty/session_tree.py`:
  the shell, its descendants, and every process in its session, each pinned
  by pid and birth). That PR deliberately left the normal stop's closure
  alone.
- **A job that ignored HUP and TERM held the whole stop** until the deadline,
  and then failed it. The only way out was `--force`, which skips the closure
  notices and the drain guarantees.

The user ruled on 2026-09-27 ("scope completion plus a kill after the
grace"). Stop and update are forced kills that owe the owner a closure notice;
"graceful" describes only the TTL reaper's reclamation. This matches how the
new updater closes terminals at release (FC-6).

## Decision

- **Membership.** A normal stop captures each terminal with the same rule
  as #3521: the shell, its descendants, and every process in its POSIX
  session, each pinned by pid and birth, before any signal is sent. A session
  with anything beyond its shell is busy.
- **Sequence.**
  1. SIGHUP to every shell first. This stops restart loops from spawning,
     which is the #2045 ordering.
  2. SIGTERM to every other captured member.
  3. Wait at most `_TERMINAL_STOP_GRACE_S` (10 s), capped by the stop's own
     deadline. While waiting, new descendants of live members join the
     capture.
  4. SIGKILL whatever is still alive through `session_tree.kill_session_tree`,
     session by session. That call freezes the members first, rescans, kills
     children before parents, and kills the shell last.
- **Timing.** The SIGKILL leg runs even when the grace used up the stop's
  deadline: a stop that reached its terminal phase closes its terminals. That
  leg is bounded by `_TERMINAL_KILL_WAIT_S` (3 s) per wait. A process that
  outlives its SIGKILL still fails the stop with the per-process inventory,
  and the hold stays.
- **Notices.** A busy session that is verified closed gets the existing
  durable closure notice, including when the SIGKILL was what ended it.
- **Scope.** Only terminal closure changes. `ava pause` (and the updates and
  restarts built on it) never closes a terminal, so its behaviour is
  unchanged. The service stop and the data-plane stop keep their
  no-escalation contract. `--force` keeps its own path.

## Alternatives rejected

- **Keep no escalation and only widen the capture.** The orphan would then
  be seen, but a HUP/TERM-ignoring orphan would fail the stop after up to
  300 s. The operator would be left with `--force`, which gives up the
  notices. That turns the ruling's case into a failed stop instead of a
  closed terminal.
- **SIGKILL at once, with no grace.** A job that handles TERM would lose
  its cleanup, and the shell would get no chance to pass its own hangup to
  its jobs. The grace costs at most 10 s, and only when something ignores
  the signals.
- **Escalate at the stop's deadline instead of after a fixed grace.** A
  stuck job would still cost up to the full 300 s drain budget before
  anything happened. The grace should be the terminal's own bound, not
  whatever budget the drain left over.
- **Route the closure through each host's `kill --graceful` op.** That op
  SIGTERMs the shell, which an interactive bash ignores. It also runs one
  session at a time behind a 5 s wait each. The stop needs the #2045
  shells-first HUP, delivered to all sessions at once.
- **Also escalate the service and data-plane stops.** Those stops protect
  checkpoints, drain receipts and disconnect bookkeeping
  (decisions/2026-09-12-stop-window-contract.md). A forced kill there
  destroys state the next start depends on. A shell job has no such
  contract.

## Consequences

- The old principle "a normal stop never force-kills" no longer covers
  terminals. The stop log, the incomplete-stop message and the docs now say
  that services and the data plane are not force-killed.
- A stop whose drain consumed nearly all of its budget still spends the
  SIGKILL leg. It can overrun its deadline by that bounded leg, and a later
  phase then reports the expired deadline. A retry finds the terminals
  already closed.
- After the shell has exited, the SIGKILL leg finds new members only as
  descendants of captured processes. The session-id scan runs only while the
  verified shell is alive (#3521's rule). So a process that a member forks
  and orphans during the grace, after the shell is gone, and whose parent
  exits before the next 50 ms poll, can still escape. Pre-existing orphans,
  and anything a surviving member still parents, are taken.
- A Windows unit's terminals still go through the non-escalating service
  stop (`stop_services`). A POSIX session has no Windows counterpart, so
  extending the rule there needs its own membership rule.
- A member this user may not signal (for example a root `sudo` on the pty)
  outlives the SIGKILL. The stop reports it and keeps the hold.

Forward: [decisions/2026-09-28-session-id-proven-by-a-live-member.md](../sessions/2026-09-28-session-id-proven-by-a-live-member.md)
closes the escape in the third consequence above (a process forked and
orphaned after the shell is gone), and a closed session's notice is now
recorded even when another session leaves the stop incomplete.

Forward link (2026-10-03): `ava pause` was deleted; a stop with a different keep set replaces it. See [delete ava pause](../../../agents/graph/2026-10-03-delete-ava-pause.md).

Forward link (2026-10-03): the closure described here is run by the
[pty-sessions service](../sessions/2026-10-03-pty-sessions-service.md) on a `close_all` request
(`base/sessions/pty/closure.py`) instead of by the stop process against each session's host; the
sequence, grace and notices are unchanged. The "each host's `kill --graceful` op" alternative
rejected above no longer exists.
