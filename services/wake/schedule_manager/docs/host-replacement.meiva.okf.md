---
type: doc
title: Schedule recovery across a session host replacement
description: Why a resident schedule is relaunched once when the process hosting its session is replaced, what stays terminal, and how the manager behaves while that host is absent.
tags: [gateway, schedules]
---

# Schedule recovery across a session host replacement

```meiva-contract
id = "host-replacement-restart"
kind = "invariant"
statement = "An enabled running schedule whose launch belongs to a previous generation of the session host is relaunched once in the current generation without a new desired revision, through the same one-attempt launch latch and capped per rolling window. A silent death inside the current generation, an attempt whose generation is unknown, and every schedule that is not running stay terminal errors."
code = [
  "services/wake/schedule_manager/manager.py:ScheduleManager",
  "services/wake/schedule_manager/manager.py:INTERRUPTED_HOST_REPLACED",
  "services/wake/schedule_manager/manager.py:auto_restart_max_per_window",
  "services/wake/schedule_manager/manager.py:auto_restart_window_seconds",
  "services/wake/schedule_manager/host_restart.py:AutoRestartLedger",
  "services/wake/schedule_manager/host_restart.py:emit_auto_restart",
  "services/wake/schedule_manager/host_restart.py:emit_auto_restart_suppressed",
]
tests = [
  "services/wake/schedule_manager/tests/test_manager_state.py::test_a_replaced_session_host_relaunches_the_schedule_once",
  "services/wake/schedule_manager/tests/test_manager_state.py::test_a_second_silent_death_in_the_same_generation_is_terminal",
  "services/wake/schedule_manager/tests/test_manager_state.py::test_an_attempt_from_before_the_column_is_not_relaunched",
  "services/wake/schedule_manager/tests/test_manager_state.py::test_the_window_cap_stops_relaunching_a_host_that_keeps_restarting",
  "services/wake/schedule_manager/tests/test_manager_state.py::test_a_refused_launch_does_not_consume_the_auto_restart_budget",
]
```

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

```meiva-contract
id = "session-host-absent"
kind = "invariant"
statement = "While the session host does not answer, one reconcile pass takes no action: no session is declared lost, no schedule changes status, and no launch is attempted."
code = ["services/wake/schedule_manager/manager.py:ScheduleManager"]
tests = [
  "services/wake/schedule_manager/tests/test_manager_state.py::test_an_unreachable_session_host_marks_nothing_lost",
]
```

The session layer reads an absent service as "no sessions" — true, because its
sessions ended with it — and that answer would otherwise mark every resident
schedule lost on the first tick of a service restart. The manager therefore asks
for the generation first: `None` means unknown, and unknown takes no irreversible
action. The skip is logged at most once per
`HOST_GENERATION_UNKNOWN_LOG_INTERVAL_S`.
