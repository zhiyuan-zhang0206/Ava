---
type: doc
title: Retained release transition
description: One captured release request and durable journal drive ordinary root lifecycle from a finite external executor.
tags:
- cluster-lifecycle
- release
---

# Retained release transition

`ava cluster update --prepared REQUEST` submits or resumes one immutable release
operation. The CLI has no moving-main, mutable-checkout or per-service update
branch. Submission success describes native dispatch/readback, not a completed
upgrade. The operation journal supplies its actual phase and chosen direction.

## Authority and inputs

`request.py` captures the home, registry, machine, configuration digest, operation
generation, and exact previous/candidate/executor artifact, manifest, source and
schema identities. The candidate supplies the retained executor. Full image
verification includes packaged source identity and every up/down migration SQL
file; an unchanged squashed baseline alone does not establish equal SQL.

`identity.py` requires the existing initialized start intent to match the exact
captured registry reservation. Configuration admission uses settings-free
`shared/start_inputs.py`, before Settings and at effect/readiness boundaries.
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

`execute.py` drives the journal's phases through `local.py` effects; the
details, including recovery direction and root start, are in
[[cli/release_transition/execution.ava.okf.md]].

## PITR operations

`ava cluster pitr activate` and `rollback` submit a `PitrRequest` through this
same home journal and finite native adapter. A request captures one selected
image, activation identity, action generation and exact configuration inputs;
it has no release direction or selector step. The home is reserved before any
activation record, environment or PostgreSQL configuration mutation. A new
operation after a completed release captures the current image, while an
interrupted operation continues its original captured image.

`ActivationRecord` remains the sole business journal: logical recovery snapshot,
archive settings, exact WAL ACK and viewer evidence, base candidate and restore
proof. The home journal records exact business-write byte intent, maintenance
generation, start-input seal and native data-plane custody. Capability is local
to the admitted executor, bound to home/action/generation, and never inherited
through environment flags. Ordinary start, maintenance and configuration writes
refuse an incomplete operation.

The PITR phases are prepared, provisioning, quiescing, stopping_apps,
stopping_data, starting, observing, resuming, proving and complete. Provisioning
validates the inactive posture and recovery snapshot, then journals each owned
archive mutation and one atomic four-flag environment CAS. Its start seal binds
configuration, auto.conf, expected archive settings and the current PostgreSQL
birth. The executor replaces itself with its exact recorded image argv and
environment to load fresh Settings; PID and native invocation remain unchanged.

After the maintenance drain, root application closure precedes data-plane
closure. The complete PG/Redis/PgBouncer birth/tree receipt is persisted before
the first data signal. A retry with the database down checks that same receipt
without another SQL drain; a vanished leader does not excuse a live captured
child. Startup uses the ordinary boot owner and the same image. Full selected
readiness and a new PostgreSQL birth with the sealed settings precede resume;
WAL, remote-viewer and restore proofs then establish protection.

Rollback is explicit and serialized after the exact previous executor domain
is positively closed and retired. A still-held maintenance timestamp is retained;
a new one is captured only after the recorded resume boundary. Offline rollback
requires the stopped hold, root absence and durable data custody before restoring
exact owned auto.conf bytes. Unknown writes or process ownership remain blocked.
Rollback keeps config-owned PITR service flags and all backup data. Native PITR
activation/rollback proof is still required; source controls are not that proof.

Every online `ALTER SYSTEM` intent binds exact preimage and predicted PostgreSQL
17 postimage digests. PostgreSQL's own file parser supplies ordered persisted
values; effective `SHOW` values belong to post-restart readiness. The admitted
input is native `ALTER SYSTEM` formatting (or an empty initial file), so the
predicted rewrite cannot flatten unclassified includes or adopt manual byte
changes. An interrupted write accepts only its exact postimage; the next write
requires the last owned digest. This does not fence an independent SQL or file
writer between observations: divergent output remains a blocked operation.

A repeated protected/rolled-back action verifies the business record against
its original completed home journal, including action and generation. It joins
that receipt even after a later release; it neither reserves a new operation nor
switches images. Missing or changed completed evidence refuses before effects.

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

The current effect adapter admits an initialized, single-host gateway (Linux,
or macOS through the persistent home helper) with a local data plane, equal
packaged SQL, and no retained terminal writers. Other topologies or schema
changes refuse before quiescing. A schedule or PTY may write the DB after
application root exits; root closure is not fleet writer closure. PITR is never
admitted on macOS. Native Windows execution, fleet/schema barriers, workload
rollback policy and complete application A/B/A proof remain in the
[lifecycle implementation plan](../../future/infra/unified-cluster-lifecycle.md).

The older HTTP/publication orchestration still being removed is not a fallback
of this entry. Its remaining callers must be deleted before cutover.
