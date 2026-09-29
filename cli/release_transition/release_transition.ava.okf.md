---
type: doc
title: Retained release transition
description: One captured release request and durable journal drive ordinary root lifecycle from a finite external executor.
tags:
- cluster-lifecycle
- release
---

# Retained release transition

`ava cluster update --prepared REQUEST` hands the request to its verified
executor image ([[cli/release_handoff/release_handoff.ava.okf.md]]), which
submits or resumes one immutable operation (no moving-main, mutable checkout
or per-service branch). Success is native dispatch/readback, not completion.

## Authority and inputs

`request.py` captures the home, registry, machine, configuration digest, operation
generation, and exact previous/candidate/executor artifact, manifest, source and
schema identities, as kind `fleet`, `unit` or `pitr`. The candidate supplies
the retained executor. Full image
verification includes packaged source identity and every up/down migration SQL
file; an unchanged squashed baseline alone does not establish equal SQL.
No platform is captured: each verification, boot included, checks the image's
ABI tag against the host as observed then ([[shared/deploy/release/runtime_release.ava.okf.md]]).

`identity.py` requires the existing initialized start intent to match the exact
captured registry reservation. Configuration admission uses settings-free
`shared/deploy/release/start_inputs.py`, before Settings and at effect/readiness boundaries.
It includes desired service selection. These comparisons detect changed inputs;
they do not claim a lock over independent configuration writers.

`journal.py` owns release decisions. `$AVA_HOME/updates/active` names one bounded,
atomically written operation; the home operation lock serializes execution.
Intent precedes effects, mutation compares the complete prior record, and a
retry cannot replace captured inputs. Serialized writes enforce the reader's
byte limit before publication. Exhausted capacity preserves readable evidence
and refuses further effects.

Initial admission checks the captured predecessor against the selected release
inside that same home lock, before publishing a new operation. Exact journal
replay does not repeat initial admission: the operation may already have selected
its candidate. Quiescing rechecks the predecessor immediately before creating
the maintenance hold or draining work, so a selector changed outside the journal
cannot authorize disruption of a different release.

## Execution

`execute.py` runs the fleet coordinator (or a unit follower, or PITR) over
`local.py`'s one-home effects: [[cli/release_transition/execution.ava.okf.md]].
Each direction's database write generations:
[[cli/release_transition/write-generations.ava.okf.md]].

## PITR operations

`ava cluster pitr activate` and `rollback` submit a `PitrRequest` through this
same home journal and finite native adapter:
[[cli/release_transition/pitr/pitr.ava.okf.md]].

## Executor custody

The native adapter owns executor birth, observation, and retirement. `native.py`
is the one dispatch point: a recorded launch uses the adapter of its required
`kind`, written by every launch plan; a missing or unrecognized kind refuses,
never a default, both at dispatch and on journal read/validation. A new launch
uses the host's adapter or refuses. Linux is described in
[[cli/release_transition/launcher_linux.ava.okf.md]], macOS in
[[cli/release_transition/launcher_macos.ava.okf.md]]. The journal keeps each
kind's native evidence and closure rules distinct. An uncertain dispatch
cannot be repeated. Continuing a positively closed executor preserves the
request, phase, and release direction, and retains its native evidence under a
new attempt number. Deletion intent and observed unit/cgroup absence precede
archiving an attempt. The submitting CLI retires a completed executor from
outside that executor; a process cannot certify its own closure.

## Connected boundary

A release ([[cli/release_fleet/release_fleet.ava.okf.md]]) admits a fleet of
one: an initialized gateway home (Linux, or macOS through the persistent home
helper), local data plane, equal packaged SQL. Networked fleets (slice
dbgen-8) and schema changes refuse before effects. A schedule or PTY may write
the DB after application root exits, so the stop phase closes every persistent
terminal (see Execution); a PITR activation closes them the same way
([[cli/release_transition/pitr/pitr.ava.okf.md]]) and is never admitted on
macOS. Native Windows execution, the migration barrier and a full A/B/A proof
remain in the [lifecycle implementation plan](../../future/infra/unified-cluster-lifecycle.md).

The older HTTP/publication orchestration still being removed is not a fallback
of this entry. Its remaining callers must be deleted before cutover.
