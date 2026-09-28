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
responsible process). Stop is certified only once the leader is reaped and the
kernel reports that group empty (`process_group_closure.group_empty`): a
child forked during TERM must exit too, or stop refuses with its PID; only force
kills it. `setsid()` escapes by construction; exec domains keep their own groups.
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

Unit exit before stop: root reaps its own leaders, and at that reap it reads
whether the leader's group was empty. The custody record still blocks a cold
duplicate root and automatic revival. A later stop or down first confirms by
native birth that the recorded leader is not live (gone, a zombie, or its PID
now another birth); a birth it cannot verify keeps custody and names the
record and next step. Confirmed dead with an empty group at reap, custody is
released without a signal. A PID is never reused while it is still the
process-group ID of a live group (POSIX), so a process now holding that PID
proves the unit's group ended: release it and never signal that stranger or
the group it leads. Otherwise descendants outlived the leader; ordinary group
closure stops them. Released custody lets root exit on TERM alone, as the
Linux boot unit's `SendSIGKILL=no` requires.
