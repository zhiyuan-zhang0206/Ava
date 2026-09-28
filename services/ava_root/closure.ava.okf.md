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
new service or resource birth remains closed. An operator can inspect the same
owner, explicitly close its captured domains, then request shutdown again.
Native birth checks reject PID reuse. Missing IPC is unknown, never proof of
absence or readiness. Terminal and execution resources keep their own existing
ownership contracts and are not renamed application service sessions.

Linux `KillMode=process` deliberately signals root alone so independently owned
Postgres, Redis, and PgBouncer siblings can survive application-root shutdown.
Root must close its own application tree. An abrupt root death can leave children;
retained custody blocks cold duplicate launch, but automatic orphan recovery and
proof of independently detached execution-domain closure remain unimplemented.
The native Linux CI test exercises actual manager adoption, root TERM closure,
and retention of a data-process stand-in. It does not prove database durability
or Windows containment. Mac permission grants require real signed-helper proof.

Unit exit before stop: root reaps its own leaders. At that reap, while any
member still reserves the group number, it reads the group: empty means nothing
of the unit remained; otherwise it records every member's native birth in the
unit's custody. The record still blocks a cold duplicate root and automatic
revival. A later stop, or a retry after a refused one, that finds the recorded
leader not live (gone, a zombie, or its PID now another birth) never lists the
group again: once the recorded members exit, another program's group can carry
that number. A birth it cannot verify keeps custody and names the record and
next step. Otherwise it signals only the recorded births and their
birth-verified descendants, force staying explicit. Once none lives, custody is
released when the unit's group is proven over: empty at the reap, empty now, or
its number now held as a PID by another process (a PID is never reused while it
is still the process-group ID of a live group, POSIX), whose group is never
signalled. An occupied group with no recorded birth alive may be a stranger's:
custody stays and the stop refuses, naming the group, its PIDs and the record.
Released custody lets root exit on TERM alone, as the Linux boot unit's
`SendSIGKILL=no` requires.
