---
type: doc
title: Hold watchdog — completing an orphaned maintenance hold out-of-band
description: The OS-scheduled actor that completes a provably ownerless post-stop hold once, without the database — the condition groups that license the attempt, the completion-environment gate, the one-attempt budget (with its local note queue), and the rescued-vs-expired-complete split.
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

The attempt is GATED before it is spent (task #4080). Only a PURE
agent-runner's converge builds the gateway OTLP relay, and in the 2026-09-19
gateway-migration window the start leg died at exactly that build —
`AVA_GATEWAY_OTLP_ENDPOINT` was not yet published — burning a generation's
single attempt in a window nothing could have completed. The job therefore
resolves the endpoint the way the start leg's own boot resolves its config
(`shared.bootstrap.resolve_bootstrap_values`: fresh snapshot / live fetch /
last-known snapshot) and stands down with the attempt UNSET while it is
missing, invalid, or unresolvable — including an unresolved capability set —
re-asking the question every scheduled run. The semantics stay single-attempt:
the budget is spent only once the gate certifies the environment, deferrals
spend nothing, and a spent attempt is never refunded.

The outcome reaches the attempt CAS unconditionally; the fleet record
(`shared.host_deploy_state`) is mirrored best-effort. A settings-lite job on a
pure runner can never dial the database at all (its URL is the never-dialed
placeholder there), so a note that cannot land is queued durably in
`$AVA_HOME/state/stranded-recovery-note-pending.json` and backfilled by the
first DB-capable run — the job itself on a gateway-serving unit, a watchdog
round otherwise (`stranded_pause.sync_stranded_hold_record` flushes first).
