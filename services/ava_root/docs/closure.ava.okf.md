---
type: doc
title: Ava Root closure and custody
description: How root certifies a unit stop, retains uncertain custody, and releases the custody of a leader that exited before any stop.
tags: [services, lifecycle]
---

# Ava Root closure and custody

Root records custody before spawning and preserves captured native births before
signals. Normal stop is bounded TERM and exact observed closure; only explicit
force permits KILL. Failed closure retains custody and blocks a replacement.

POSIX units each lead a process group (setpgid; same session and macOS
responsible process). A stop that finds the leader live is certified only once
the leader is reaped inside that stop and the kernel reports its group empty
(`process_group_closure.group_empty`): a child forked during TERM must exit too,
or stop refuses with its PID; only force kills it. `setsid()` escapes by
construction; exec domains keep their own groups.
Root also keeps its control transport alive after failed ordinary shutdown;
new service or resource birth remains closed. One unit's refusal never halts
that shutdown: every unit is still stopped in order, a refused one keeping its
generation and custody, and all refusals are reported together. A refused
unit's parent is therefore stopped while that unit keeps running, whereas
`down` ends at the first refusal and leaves that unit's parents up: the one
exception to children before parents. Production never meets it. The glue
attaches every unit to root (`services/ava_root_glue/manifests.py`), so no
unit has a parent unit, and root spawns every unit itself, so even a nested
parent's stop would not reach a child unit's processes. An operator can
inspect the same owner, explicitly close its captured domains, then request
shutdown again.
Native birth checks reject PID reuse. Missing IPC is unknown, never proof of
absence or readiness. Terminal and execution resources keep their own existing
ownership contracts and are not renamed application service sessions.

Linux `KillMode=process` deliberately signals root alone so independently owned
Postgres, Redis, and PgBouncer siblings can survive application-root shutdown.
Root must close its own application tree. An abrupt root death can leave children;
retained custody blocks cold duplicate launch, but automatic orphan recovery and
proof of independently detached execution-domain closure remain unimplemented.
The native Linux CI test exercises actual manager adoption, root TERM closure,
and retention of a data-process stand-in. It does not prove database durability.
Mac permission grants require real signed-helper proof.

Unit exit before stop: root reaps its own leaders, then reads the group a few
event-loop turns later (asyncio's child watcher reaps with `waitpid` first, on
macOS and Linux alike). A live member keeps the number reserved, so empty means
nothing of the unit remained; otherwise root records each member's native birth
in the unit's custody. Only a group that empties inside that window, its number
then taken by another process, misleads the read. A member whose birth root
cannot read is left out alone, never signalled, and keeps the group occupied.
A leader can also exit and be reaped before root reads its birth (a unit whose
`cd` or binary fails exits within milliseconds, and asyncio's child watcher can
reap it before that read). Root, its only reaper, still knows it exited, and its
group is the PID it was born leading, so the same read follows its reap and the
same stop below applies; only its own birth is missing from the record.
The record still blocks a cold duplicate root and automatic revival. A later
stop, or a retry after a refused one, that finds the recorded leader not live
(gone, a zombie, or its PID now another birth) never signals by the group
number: once the recorded members exit, another program's group can carry it,
so nothing signalled, captured or adopted comes from a group listing, which
only reads the group's session or names it in a refusal. A birth it cannot
verify keeps custody and names the record and next step. Otherwise it signals
only the recorded births and their birth-verified descendants, force staying
explicit. Once none lives, custody is released when the unit's group is proven
over (`group_scope.group_over`): empty at that read, empty now, its number now
held as a PID by another process (a PID is never reused while it is still the
process-group ID of a live group, POSIX), or the group carrying it now in
another session. That last proof holds because root leads its own session in
every launch (the direct start's `start_new_session`, which the Linux boot
unit's start also uses, and the macOS helper's `POSIX_SPAWN_SETSID`), units
only change group, and POSIX keeps a group inside one session: a classic
daemon (fork, setsid, fork) that later takes the number holds no process of
the unit. One member decides, its session read before its group and both
bracketed by its birth; a failed read proves nothing. None of these groups is
ever signalled. A group still occupied in root's session, or in one root
cannot read, with no recorded birth alive may hold unrecorded processes of the
unit: custody stays and the stop refuses, naming the group, its PIDs and the
record.
Moving that record aside is the operator's word that no process of the unit
remains: with no recorded birth alive, the next stop, or root's own TERM, drops
the generation without a signal; a live recorded birth keeps the refusal.
Released custody lets root exit on TERM alone, as the Linux boot unit's
`SendSIGKILL=no` requires.
