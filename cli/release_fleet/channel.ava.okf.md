---
type: doc
title: Fleet release coordinator channel
description: Remote units in a fleet release - dispatch through the frozen handoff, the authenticated coordinator listener, unit barriers, the unit follower, and what slice dbgen-8 plugs in.
tags:
- cluster-lifecycle
- release
---

# Fleet release coordinator channel

How the coordinator ([[cli/release_fleet/coordinator.ava.okf.md]]) and a
remote unit's executor talk. Remote units cannot take part yet: the
submission gate (`inventory.require_topology`) refuses them, naming slices
dbgen-8 (per-unit write-generation delivery) and FC-9 (converge). The code
below is complete and tested in process through that gate.

## Dispatch and preflight (`units.py`)

While a unit's root is up, the coordinator reaches it only through FC-5's
frozen handoff: `release_image_exec` at the URL the unit's `machine_units`
row advertises, running the unit's candidate image on the unit's own
`UnitRequest` document. `preflight` (at `prepared`) must answer
`{"ready": true}`; `submit` (at `dispatching`) creates the unit journal and
launches the unit's finite executor. `entries.py` holds the two read-only
answers: `receipt` (a `UnitReceipt`: identity, roles, ABI tag, adapter,
selection, verified candidate, SQL inventory and configuration digests,
enrollment id; `UnitReceipt.spec()` is its `UnitSpec`) and `preflight` (the
unit request passes the gates its submit applies, its selection is the
predecessor, no other operation is incomplete, its enrollment is the one
named).

## Listener and client

`listener.py` binds the captured endpoint (the gateway home's reserved
`coordinator` port) for one coordinator run. `GET
/v1/op/<id>/unit/<key>` serves the unit's current journaled instruction;
`POST .../report` queues an answer naming that instruction's digest (only
the coordinator thread journals it); `POST .../capability` answers 501. Each
request carries an HMAC proof keyed by the unit's enrollment
(`shared.cluster.authority.channel`), checked against the gateway's current
record in a per-run replay window, so wrong operations, unknown units,
forged, replayed or skewed requests, and rotated or revoked enrollments are
refused. `client.py` is the unit side with typed failures: the coordinator
away, a stale answer, a refusal, a deferred capability.

## Instructions and barriers

The coordinator journals each instruction (`standby`, `quiesce`, `close`,
`wait`, `start` with its generation, `resume`, `watch`, `restore`,
`complete` with the outcome, `excluded`) before serving it, and reissues
only a changed order, so a continuation keeps every digest. A barrier waits
until every included unit answered or passed its journaled deadline, and at
least one re-answer window (30 s) after this run bound its listener. Before
the fence a failed or silent unit fails the barrier and the operation
aborts; after it the unit leaves the operation as `failed` or `unknown`
(never to return) and is ordered to close and stay closed (`excluded`).
At the watch window's end every unit must report again.

## Follower (`follower.py`)

The unit's executor pulls, journals the instruction as acted on (its
digest) before the effect and the answer before sending it, and repeats a
journaled answer instead of acting twice: either side's death reconciles by
digest. An older instruction is ignored, another operation's, unit's, image
or maintenance hold refused. `restore` is the coordinator's abort and a
`previous` instruction its one recovery, followed from the unit's phase.
Without its coordinator it holds its phase up to a 24 h lifetime.

## What dbgen-8 plugs in

- `CapabilityExchange.exchange` at the unit's `authorizing` step (today
  `DeferredExchange` asks the listener, which answers 501 naming dbgen-8);
- the listener's capability route and per-unit issuance at the coordinator's
  `authorizing` phase;
- relaxing `require_topology` for units that hold an enrollment.

## Before remote units (dbgen-8/FC-9)

Known defects on the remote-unit path, unreachable while `require_topology`
refuses every remote unit (found by the PR #3479 adversarial review); each
must be fixed before dbgen-8 relaxes that gate:

- **The barrier judges deadlines before draining answers.**
  `RemoteUnits.barrier` evaluates `_marks` (deadlines) before
  `_journal_answers` drains the listener's queue. At `quiescing` the
  gateway's own bounded drain runs between `instruct` and `barrier`, so a
  unit that answered in time is judged silent and the release aborts before
  the fence. Journal the queued answers at the top of every barrier
  iteration.
- **A unit's own clock stamps the evidence.** `RemoteUnits.unit_reports`
  turns the unit-supplied `Report.at` into `UnitReport.observed_at`; a unit
  clock ahead of the gateway's makes `workload._require_coherent` raise
  ("evidence cannot be newer than the verdict"), so clock skew recovers or
  holds the release. Stamp the listener's receive time instead.
- **The follower's watch answer is fake liveness.** `Follower.follow`
  re-stamps a journaled `watch` answer with a fresh `at` without observing
  the unit's root, so a dead unit keeps reporting ready through the window.
  Re-observe the root before re-stamping.
- **The unit `close` bound is below a unit's worst-case stop.**
  `Coordinator._stopping` bounds the `close` barrier by `close_s +
  cancel_grace_s + drain_s` (130 s by default), but a unit's own stop is the
  `close_s` wait, the 90 s root stop (`local.py`), the cancel grace and the
  kill legs (about 160 s), so the strict barrier aborts spuriously.
- **A negative `Content-Length` pins a listener thread.** The listener
  reads to EOF on a negative length, and its sockets have no timeout, so an
  unauthenticated peer can hold handler threads. Reject `length < 0` and
  set a socket timeout (being fixed in `listener.py` separately).

## Candidate reference

The ops kind needs a unit's candidate image reference before the gateway can
ask for its receipt. Ruled on 2026-09-27
([decision](../../decisions/2026-09-27-fleet-core-release-choices.md)):
`ava cluster release prepare` on each unit publishes its receipt to the
gateway over the machine-token API, and the request builder verifies each
through `release_image_exec(receipt)` (slice FC-7b). Until then the builder
refuses included remote units.
