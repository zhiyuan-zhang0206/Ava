---
type: doc
title: Schedule recovery across a session host replacement
description: Why a resident schedule is relaunched once when the process hosting its session is replaced, what stays terminal, and how the manager behaves while that host is absent.
tags: [gateway, schedules]
---

# Schedule recovery across a session host replacement

An enabled running schedule whose launch belongs to a previous generation of the session host is relaunched once in the current generation without a new desired revision, through the same one-attempt launch latch and capped per rolling window. A silent death inside the current generation, an attempt whose generation is unknown, and every schedule that is not running stay terminal errors.

The host is this machine's pty-sessions service; its generation is the `ping`
identity described in
[[base/sessions/pty/docs/pty_sessions/host-generation.meiva.okf.md|service host generation]],
and the shared rule is [[base/sessions/docs/host-generation.meiva.okf.md|session host generation]].
`schedules.launch_generation` records the generation each attempt was launched
into. A relaunch authorizes itself by resetting the launch latch only while the row
still matches the observed generation, so an operator's concurrent restart or a
newer revision wins the row first and the manager does nothing. It closes the
interrupted run row as `interrupted (session host replaced)`.

The relaunch is one authorization, retried every reconcile until it either comes up
or a newer revision wins: a serving-receipt refusal consumes no automatic relaunch
and emits no `schedule_auto_restart` (it reports `schedule_launch_refused`
instead), and the event fires when the process is actually observed live, carrying
both generations and the count in the window.
Exceeding `MEIVA_SCHEDULE_AUTO_RESTART_MAX_PER_WINDOW` (default five per 1800 s)
records the schedule as an error and emits `schedule_auto_restart_suppressed`.

A silent death inside one generation stays that component's own failure even when
the cause was an OOM kill or an operator's `kill -9`. Keeping that case terminal is
a choice, not an omission: it keeps a deliberate kill deliberate and a crash loop
from becoming a restart loop, and it costs one explicit restart when the cause is
unknown.

The session layer reads an absent service as "no sessions" — true, because its
sessions ended with it — and that answer would otherwise mark every resident
schedule lost on the first tick of a service restart. The manager therefore asks
for the generation first: `None` means unknown, and unknown takes no irreversible
action. The skip is logged at most once per
`HOST_GENERATION_UNKNOWN_LOG_INTERVAL_S`.
