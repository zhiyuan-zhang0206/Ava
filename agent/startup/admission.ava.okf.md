---
type: doc
title: Runtime Admission and Boot Attempts
description: Schema rejection, incarnation admission, canonical session publication and bounded restart recovery.
tags: []
---

# Runtime Admission and Boot Attempts

## Schema rejection

`claim_agent_row_or_die_on_stale_schema` validates schema before claiming running.
Both `CodeBehindSchema` and `SchemaVersionMismatch` reject boot. Only genuinely
unowned legacy idling rows become terminated: `_mark_preclaim_terminated` requires
absent runtime kind, generation, owner and command pointer, and stamps
`termination_source='launch-confirm'`. Owned attempts remain under their durable
command's deadline and attempt budget. Legacy crash resurrection can apply after
schema or placement repair. A code-behind host can update to the cluster schema;
code-ahead requires correcting the deployed code, not pretending migration ran.

## Admission and canonical publication

Parent launch records use unique boot-attempt names on POSIX and Windows. Only
the actual PostgreSQL admission winner publishes the canonical agent record,
including ordinary legacy births and explicit resurrection. A resident or
unreadable old canonical process refuses replacement; no revive/retry path kills
it by name. This preflight is not a reservation: admission CAS and the bounded
canonical publication lock protect a winner appearing after the preflight.

An applied durable restart requires its exact `--restart-command-id`. Under the
metadata row lock, `agent/restart_admission.py` checks target incarnation, pending
pointer and original application-time deadline. Missing, delayed or superseded
attempts cannot use legacy admission.

`agent/session_admission.py` publishes the winning process's canonical record
before admission commits. The record is repairable observation, not a second
ownership authority. Publication failure rolls back the DB transaction; live or
unreadable previous identities refuse replacement without signaling. The
controller launches `ava-boot-<agent>-<command>-<attempt>`; only the admitted child
publishes canonical. Attempt records do not count as admitted agents or shells.
Filesystem/DB publication is not atomic; real subprocess crash coverage remains
a deployment gate.

## Bounded command recovery

The existing restarter controller allocates `payload.launch_attempts` in a short
metadata-then-inbound transaction before spawning. Crash after commit consumes
an attempt even when no OS process started. Retry ceiling and original command
deadline both apply. Exhaustion records an explicit unobserved result, not an
invented `observed_at` or PID. Before deadline, exhaustion remains unobserved;
after deadline, positive target absence permits a failed command outcome and
fenced pointer release, never a successful observation timestamp. The ended
runtime becomes terminated; its restart failure remains in the command.

Live runtime and cold controller share `shared/lifecycle_acceptance.py`. Cold
acceptance requires proof of no admitted owner for that exact agent and an
explicit restart/terminate. A new command has its own budget; an old command is
never retargeted or reset. Ordinary chat, compact and system-note delivery cannot
revive a released failed process: watchdog selection and final pending-work
resurrection CAS both refuse. Explicit restart uses the public lifecycle action.
Legacy unowned terminated-agent policy is unchanged. Protocol advertisement and
all-old-writer closure remain activation gates.
