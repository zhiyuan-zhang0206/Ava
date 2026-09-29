---
type: doc
title: Release transition execution
description: Phase-by-phase release effects, recovery direction, root start through the platform's persistent root owner and fresh readiness before resume.
tags: [cluster-lifecycle, release]
---

# Release transition execution

`execute.py` runs a fleet operation's coordinator
([[cli/release_fleet/coordinator.ava.okf.md]]; a single box is a fleet of one),
a remote unit's follower, or PITR's own phases. The coordinator advances
prepared, dispatching, quiescing, stopping, fencing, selecting, authorizing,
starting, observing, starting_units, resuming, watching and complete; each
phase reconciles actual state. A failure before the fence aborts (`restoring`:
the unchanged previous image on the unchanged generation). A candidate failure
at start, observation, the start barrier or the watch window chooses the
captured predecessor once, closes the candidate, then uses the same
fence/select/authorize/start/observe path. Recovery never chooses another
target or loops between releases. A failure while fencing, selecting,
authorizing, resuming or restoring holds for continuation: none is an
automatic rollback boundary. Every executor (coordinator, follower, PITR)
treats any `Exception` as a failure and journals it before routing it
(`failure.py`); only a process-ending `BaseException` passes undecided.

The executor's `main` opens its log sinks first (`shared.log.init_cli_process`,
name `release-executor`): stderr, kept by the native adapter (the transient
unit's systemd journal; launchd's `updates/<id>/executor/a<N>/stderr.log`),
`$AVA_HOME/logs/release-executor.log`, and the event pipeline (never the
database). A routed failure's traceback lands there.

While it runs, the executor stamps `updates/<id>/executor-heartbeat` from a
thread every `LEASE_RENEW_INTERVAL_S` and removes it when it leaves
(`shared/deploy/release/operation.py::executor_heartbeat`). The health probe lets an
incomplete operation explain an outage only while that stamp is at most
`EXECUTOR_HEARTBEAT_TTL_S` old; before the first beat, the stamp the
submission and each native dispatch leave (`open_launch_grace`). Past it the
executor is lost (killed, OOM'd, rebooted or never launched) and the probe
alerts `operation executor lost`, graded from the last stamp. A stale stamp
is lost whatever error the journal records: every exit removes the stamp, a
hold's included, while an abort or recovery decision's error stands until
its next phase with the executor still running.

The stop phase closes this unit's writers. Persistent terminals — agent
shells, coding sessions, watchers, page and schedule runners — do not survive
a release ([decision](../../decisions/2026-09-27-fleet-release-and-cutover-policies.md)
item 2). While root still serves them, the phase waits the captured policy's
`close_s` for their jobs to finish, signalling nothing. It then stops root, keeping
terminals, so no reconciler re-arms a session. `close_release_terminals`
(`cli/commands/lifecycle/service_stop.py`) captures every recorded shell, every other
member of its session (`shared/sessions/pty/session_tree.py`: descendants and
POSIX session, a double-forked job included) and each PTY host birth, records
the `ava stop` closure notice for each busy session's owner (naming the
release, before any signal), HUPs shells and TERMs jobs, and after the
policy's `cancel_grace_s` kills each session whole through `session_tree`
(frozen, children first, shell last). A session whose shell the kill ended is
recorded again under the same dedup key, naming whatever of it outlived its
SIGKILL, so its owner still gets one notice. A PTY host still running after
its session is gone gets SIGKILL to its captured birth. This is the same closure
`ava stop` runs, with the release's bounds. Closure is
the kernel observation that each is gone and no recorded terminal is live;
selection checks that evidence again. A survivor fails the phase with its
identity. After start, the schedule manager re-arms schedules and the page
server re-arms open pages; other sessions stay closed, and their owners learn
it from the notice.

`local.py` owns those release effects and selects the native root owner by
the operation's recorded executor kind. On Linux `root_service.py` uses the
existing home boot unit, never a second application supervisor. Its
one-operation start action runs `stage.py` in that unit; the new root therefore
survives the finite executor's exit. On macOS the persistent home helper births
root: [[cli/release_transition/root_macos.ava.okf.md]]. Readiness checks every
selected service plus native root identity. Resuming checks fresh readiness
again, including after an interrupted executor or a previously written resume
receipt. A durable serving marker is not a substitute for a live observation.

`stage.py` admits the captured image and uses ordinary `run_start`. Release
startup does not mutate an editable checkout, install dependencies, migrate, or
silently prepare external assets. After observation, `boot.py` becomes the
steady pinned-image boot action on Linux; on macOS the keeper's pinned seed is. `shared/deploy/release/operation.py` blocks unrelated
ordinary startup while an operation remains incomplete. Only the exact current
starting journal revision grants the in-process start capability; child
processes do not inherit it.

## Write generations

Every direction fences the active database write generation and runs on a
fresh one; the finite executor dials as the OS-user administrator:
[[cli/release_transition/write-generations.ava.okf.md]].
