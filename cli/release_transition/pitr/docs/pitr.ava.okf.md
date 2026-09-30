---
type: doc
title: PITR activation and rollback transition
description: PitrRequest drives the same home journal and finite native adapter as a release, through its own prepared/provisioning/quiescing/stopping_apps/stopping_data/starting/observing/resuming/proving phases, with ActivationRecord as the sole business journal and native ALTER SYSTEM byte-exact custody.
tags: [cluster-lifecycle, release, pitr]
---

# PITR activation and rollback transition

`ava cluster pitr activate` and `rollback` submit a `PitrRequest` through this
same home journal and finite native adapter
([[cli/release_transition/docs/release_transition.ava.okf.md|release transition]]).
A request captures one selected image, activation identity, action generation
and exact configuration inputs; it has no release direction or selector step.
The home is reserved before any activation record, environment or PostgreSQL
configuration mutation. A new operation after a completed release captures the
current image, while an interrupted operation continues its original captured
image.

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

PITR stays single-box: preflight and quiescing hold its reserved gateway home
to the fleet inventory's single-unit gate (`inventory.require_fleet_of_one`,
[[cli/release_fleet/docs/coordinator.ava.okf.md|fleet coordinator]]). They admit a
live persistent terminal or schedule — production always has them, so refusing
would make activation impossible there
([decision](../../../../decisions/2026-09-27-unit-join-pitr-closure-fleet-policy.md)
item 2). After the maintenance drain, `stop_apps` closes them exactly like a
release's stop phase: a bounded wait for their work to finish while root still
serves them, root stop keeping terminals alive so no reconciler re-arms a
session, `close_release_terminals` closing every recorded shell/job/PTY-host
birth with a PITR-named closure notice for each busy session's owner
(`pty_close_notices.PITR_REASON`, before any signal), then `require_no_terminals`
as the post-closure evidence check. Root application closure precedes data-plane
closure: the complete PG/Redis/PgBouncer birth/tree receipt is persisted before
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
