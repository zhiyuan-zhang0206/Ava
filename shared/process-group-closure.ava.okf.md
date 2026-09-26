---
type: doc
title: Process-group closure core
description: The one standard-library primitive that proves a launched process group closed (SIGKILL rounds, a non-reaping leader-exit wait, a kernel group listing), shared by exec domains, PITR custody, release preparation and ava-root unit stop.
tags:
- shared
- process
---

# Process-group closure core

`process_group_closure.py` imports only the standard library (a bare
interpreter runs it; `tests/lifecycle/images/test_stdlib_boundary.py` guards
that), so every group closure in the repo shares it.

## Contract

A launch leads its own process group, and its direct child stays **unreaped**
until closure is proven: the zombie keeps the group number reserved, so a group
signal reaches only this launch. `confirm_closure(process, deadline,
signal_group=None)` repeats one round:

1. one group-wide SIGKILL;
2. a wait for the leader's exit that never reaps it (kqueue `NOTE_EXIT` on
   macOS, `waitid(WNOWAIT)` on Linux), so the leader has no fork in flight;
3. a group listing: `proc_listpids(PROC_PGRP_ONLY)` on macOS, one kernel
   snapshot under the proc-list lock; a `/proc` scan on Linux, sound after the
   SIGKILL because a racing fork either fails or hands its child the signal.

Only a listing of the exited leader alone closes. Any other listed member, live
or zombie, forces another round: XNU lets a member inside fork() when the signal
lands finish it, and that child never receives the signal. At the deadline the
round raises `GroupClosureUnresolvedError` (a `TimeoutError`); a listing that
lost the leader is a `RuntimeError`. The leader is never reaped here, so custody
stays with the caller.

The default signal refuses a reaped leader and accepts XNU's EPERM for an
all-zombie group, since the listing decides. `close_unadmitted` adds the reap
and, on any failure, keeps the unreaped leader referenced for the life of the
process. `wait_group_finished` is natural completion (leader exited, listing
names nothing else) without signal or reap; on Linux it is no closure proof, so
callers close afterwards. `group_empty` answers for a group whose leader was
already reaped: the macOS listing, or a Linux null group signal, which the
tasklist lock orders against fork.

This is trusted-tool cleanup, not a fence: a member that calls `setsid()` or
`setpgid()` leaves the group.

## Callers

| Caller | Uses |
|---|---|
| `ExecProcessDomain.close_confirmed` | `confirm_closure` with its own round signal: under the domain lock and `Popen`'s wait lock, only while the root's native birth and parentage hold; EPERM passes only when no member is live |
| PITR unadmitted launch (`ExecDomainBirthError`, held retries) | `confirm_closure` with the default signal |
| Release preparation (`runtime_prepare._run`) | `wait_group_finished`, then `close_unadmitted` |
| `posix_command.run_owned_command` | `wait_group_finished`, then the exec domain's close |
| ava-root unit stop | `group_empty` and `group_members`, after the unit leader is reaped |

Consumers: [[incarnation-resources.ava.okf.md|exec incarnation resources]],
[[services/pitr/operation-custody.ava.okf.md|PITR operation custody]],
[[runtime_prepare.ava.okf.md|runtime preparation]].
