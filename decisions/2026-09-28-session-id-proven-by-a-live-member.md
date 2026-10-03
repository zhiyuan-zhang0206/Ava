# A session kill proves the session id with a live member, not the live shell

## Context

A PTY session kill (`shared/sessions/pty/session_tree.py`, PR #3521) takes the
shell, its descendants, and every process in the shell's POSIX session. #3521
scanned by session id only while the verified shell was alive. Once the shell
has died, a later session could in principle carry the same id.

`ava stop` then gained a SIGKILL leg after a bounded grace (PR #3558,
decisions/2026-09-28-stop-escalates-to-sigkill.md). The adversarial review of
#3558 (https://github.com/zhiyuan-zhang0206/Ava/pull/3558#issuecomment-5858247325)
found an escape that happens every time, not by a race. A foreground job's
TERM handler forks a helper and exits at once:

- the helper is born after the stop's signals;
- its parent is gone before the next 50 ms poll;
- the shell has already died of its hangup.

No capture rule reaches the helper, yet it is still in the shell's session. The
stop returned 0 and recorded the session as closed, in 3 of 3 runs. This held
both when the helper ignored HUP/TERM and when it kept the default
dispositions, in which case it never receives any signal.

The user ruled that a stop takes the whole session, background jobs and
double-forked orphans included, so this helper must die.

## Decision

A pass takes a process by the session id S only when S is proven, after every
read of the pass and before any signal. Two proofs count.

- **A live witness (kernel-backed).**
  - At any instant, at most one session carries a given id:
    - Linux `free_pid` returns an id only once no task holds its `struct pid`
      as PID, thread group, process group or session;
    - XNU `forkproc` skips a candidate pid that `pfind`, `pgfind` or
      `session_find` still resolves.
  - A captured member that reads S after the pass's reads, and whose identity
    is re-verified afterwards, stayed in one session for the whole pass.
    Leaving a session takes `setsid`, which renames the leaver's session to
    its own pid.
  - The frozen shell is the usual witness. When the shell is dead, any other
    captured member can serve.
  - If some other process holds the shell's pid, S is disproven: the kernel
    released that pid, so the original session has ended.
- **A fresh proof (the repo's existing pid-reuse bound).**
  - Once the helper's parent exits, the helper is the only process left in
    the session and none of the captured members are alive. No kernel fact
    observable at that moment separates it from a member of a new session that
    received S after the pid was recycled. Only elapsed time does.
  - So a proof stands for `_PROOF_FRESH_S` (1 s) after it was last renewed.
    That is half the "pid reuse cannot land inside a couple of seconds" bound
    that `shared/proc_tree.py`'s identity check already relies on.
  - A witness renews it. So does a proven pass that read any process in the
    session: that read shows the session alive after the scan began, even if
    the process exited before it could be pinned. A chain of short-lived
    processes thus keeps the proof current while passes keep reading its hops.

A process a pass cannot prove is logged with its pid and command name, once
per kill or per stop, and left running.

The stop keeps its proof current while it waits:

- Each grace poll refreshes every session's capture with one shared scan
  (`session_tree.refresh`), adding new descendants and proven session members.
  A capture nothing can prove any more is still scanned, so a process left in
  its session is logged.
- The scan reads session ids last, after the psutil pass, with a bare
  `getsid` per pid. The sweep takes about 0.2 ms over some 800 processes,
  against about 16 ms for the psutil pass. A fork-and-exit hop lives a few
  milliseconds: swept last, it still exists when read, and the pass pins it
  (in a kill, freezes it) about a millisecond later. Read inside the psutil
  pass, it was already gone.
- The sweep runs from the highest pid down. That reads the newest processes
  first only until pids wrap; after a wrap the newest are the lowest and come
  last, still within the sweep's fraction of a millisecond. The ordering is a
  small help, not what makes the scan work.
- A poll is quiet only when its scan read no non-zombie process in the
  session, pinned or not, and no captured process lives. The caller's own
  process does not count: a stop run from inside a session it closes would
  otherwise hold the grace open for itself. A process the caller may not
  signal does count; the kill reports it. A member can fork while that scan
  runs, so a quiet poll counts only after a second, immediate poll is quiet
  too.
- The kill leg starts with one more refresh and passes each capture's proof to
  the kill.

Closure notices refine #2044's rule that partial success notifies only what
actually closed: "closed" is judged by the shell, which is the session as its
owner uses it. Once the shell is gone, the agent can no longer use the
session, and whatever outlived the SIGKILL is out of its reach too.

- A busy session whose shell identity is verified gone records its notice
  before an incomplete stop reports. A retry can no longer see it, because
  its record is gone.
- If processes of that session outlived the SIGKILL, the notice names them by
  pid and command name. These are typically another user's (a root `sudo`),
  which neither the stop nor the agent may signal. The stop still reports
  incomplete and keeps its hold. This matches the host's kill op, which
  answers `ok`, `interrupted` and the survivors once the shell is gone.
- A session whose shell still lives records nothing, and a retry sees it
  again.
- The survivor list is not part of the outbox's dedup key (machine, agent,
  session, shell birth). Recording the same shell again rewrites the one
  record, and its delivery claim yields one inbound.

## Alternatives rejected

- **A witness rule alone** ("no live witness means the session is empty"):
  - The premise is false in exactly the reported case: the helper is
    uncaptured and is the session's only process.
  - The helper still escapes.
- **The review's process-group rule** (`sid == S` and `pgid` in the captured
  groups):
  - It needs the same proof, because the helper is the only member of its
    captured group, so a recycled group id looks the same.
  - It also misses a helper that moves to its own group.
- **Freezing the shell through the grace instead of hanging it up:**
  - This would keep a kernel-backed witness alive.
  - But it changes the ruled HUP-first sequence (#2045), so the shell would
    never handle its own hangup.
  - That is a separate ruling.
- **Having the PTY host hold the shell's zombie** (which pins the pid): this
  changes the host's reap protocol and couples the stop to the host.
- **Scanning by session id with no proof:** this can kill a process in a new
  session that received the recycled id.
- **Notifying only sessions whose every captured process is gone:** a session
  whose shell died but whose root `sudo` survived would never get a notice,
  because its record is gone by any retry.

## Consequences

- **What escapes.**
  - A process the stop cannot prove: no captured process alive in the session
    and no proof renewed in the last second, for example because the stop
    process stalled for over a second. It is logged once, with its pid and command name, and
    keeps the grace polling until its deadline. The stop's result is unchanged
    by it.
  - A fork-and-exit chain whose hops all die before two consecutive scans
    read any of them. Nothing logs it, because no scan saw it.
  - A chain whose hops keep forking on before a kill's freeze lands. Running
    out of freeze passes is logged; a pass that misses the current hop is not.
  - Chains of 3 ms x 60, 8 ms x 40 and 10 ms x 150 hops are taken, including
    one still forking when the kill starts, and tests lock that.
- **Theoretical corner.** The witness argument assumes that the shell's pid
  was not recycled onto one of the session's own descendants, which then led
  its own session and has itself already exited. That requires the whole
  original session to end, and the pid space to wrap, inside the kill.
- **The host's kill op.** The TTL reaper, `ava.shell.sessions.kill` and every
  other production caller reach it in its forced mode (`kill_session` and
  `kill_session_with_verdict` default to `graceful=False`, which runs the PTY
  CLI's plain `kill`). That mode freezes the live shell first, so the shell
  witnesses every pass. Only the op's graceful mode, which no production
  caller uses, lets the shell die before the sweep. It carries no fresh
  proof, so it reaches a helper forked on TERM only through a captured
  process that is still alive.
- **Kill-op latency.** A kill op waits only for the members it signalled. A
  member it may not signal has its liveness read once, so it no longer spends
  the TTL reaper's 5 s dispatch budget.
- **Identity, not pid.** Membership checks compare identities, not pids. A
  parent vouches for a child only while it still is the captured process, and
  a pid the kernel handed on never counts as its dead member.

Forward link (2026-10-03): the PTY host this decision names is the
[pty-sessions service](2026-10-03-pty-sessions-service.md) now. The kill op and the closure run
inside it, and the "host's zombie" alternative above would couple a stop to that one process.
