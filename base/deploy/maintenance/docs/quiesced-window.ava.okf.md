---
type: doc
title: Quiesced window — loops hold off, remaining clients are reported
description: Between drain and resume, local background loops stop touching the database; the stop reports the pooler's remaining clients before it stops the pooler.
status: current
---

# Quiesced window — loops hold off, remaining clients are reported

`admission.quiesced()` (phases `drained` through `ready`) is the stop window
read by every resident background loop that touches the database. The shared
round runner (`base/daemon/round_loop.py:run_rounds`) skips a round while it
holds, so the TTL reaper, the schedule manager, the ops delivery-outbox
redelivery and the delivery watchdog's recovery loops are gated by construction;
the hand-written loops (host daemon ownership renewal, the watchdog
scan, heartbeat, events-maintenance, labeler, page-server, the IM bridge's
notice poll, the memory indexer, the hierarchy tick, the gateway flushers)
check it themselves, and `scripts/content_lint/lint_quiesced_loops.py` requires
every resident periodic loop in `services/`, `gateway/` and `ops/` to gate or
carry a reasoned `# quiesce-exempt:` marker.
None of them may borrow the database across the window; an unreadable owner
reads as quiesced, the same refuse-new-work posture the journal itself enforces
by raising. The host's pending-turn scan reads the narrower
`maintenance.in_stop_leg()` (`drained` .. `stopped`): it holds through the stop
leg, but from the start leg on a booting host must drain its pending workset
even while the unit is still held — pub/sub has no replay, so recovery may
not wait for the hold to release.

The stop does not release pools: its `services` phase stops every service process,
so each pool closes with its process before the `data-plane` phase starts, and the
pooler's SIGINT stop disconnects whatever client is left. Just before that signal
the stop lists the pooler's remaining clients (`SHOW CLIENTS`: address, database,
user, state) on stderr, in the log and in the stop journal's `pooler_clients`
entry, and reports an unreadable console as such. It only reports; a runner
on another machine that is still up when the gateway stops appears there, and
the order that avoids it (runners first) is the operator's.

## Dependencies

- [[maintenance.ava.okf.md|Native pause and maintenance]] — the hold, its
  phases, and the admission gates.
