---
type: doc
title: Hold watchdog — completing an orphaned maintenance hold out-of-band
description: The OS-scheduled actor that completes a provably ownerless post-stop hold once, without the database — the condition groups that license the attempt, the one-attempt budget, and the rescued-vs-expired-complete split.
tags: []
---

# Hold watchdog — completing an orphaned maintenance hold out-of-band

## The OS-side hold watchdog: completing a hold when no in-cluster actor can (task #3887)

#3142's completion runs inside the pause controller — a watchdog round, which a
full stop kills along with the database that round reads. The 2026-09-17 S3
blackout (110 minutes, task #3719) was exactly that shape: an ownerless
post-stop hold with the OS scheduler as the only live layer. `shared/
hold_watchdog.py` plus the OS-scheduled `ava cluster hold-watchdog`
(`shared.os_hold_watchdog`; one job per home, so a two-capability box completes
its single host-level hold in one place) close the hole. When the hold's
recorded shepherd is DEAD, nothing local executes under it (no live updater
handoff / orchestration session — `hold-recover` included — / updater lock),
the held-stop marker is not fresh, no local start/stop is in flight, and the
hold has outlived its bound (the 30-minute age floor, or a declared intended
lifetime once #3724 stamps one), the job completes it ONCE via the same
stop → start → resume legs, in its own process and without the database until
`ava start` brings it back. The verdict is local-only by design — every signal
is a file, a lock, or a process probe — and missing evidence never licenses
action (an unreadable journal, an unjudgeable driver identity, an unreadable
probe all stand down). Budget: one attempt per hold generation in
`$AVA_HOME/state/hold-watchdog-attempt` (a local compare-and-set, 900s
cooldown), kill-switch `settings.gateway.stranded_hold_recovery` shared with
#3142; the two mechanisms serialize on the lifecycle lock and each other's
sessions. Semantics stay split per task #6294: a release inside the window
records "aborted (rescued within the window)"; a bound-driven completion
records "expired-complete". Output lands in `$AVA_HOME/logs/hold-watchdog.log`
(job log) and a per-attempt `hold-watchdog-<epoch>.log`.
