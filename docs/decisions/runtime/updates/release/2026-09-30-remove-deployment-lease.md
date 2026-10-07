# The deployment lease is removed

## Context

The cluster deploy lease was a TTL'd row in `deployment_state` (`holder`,
`expires_at`, `phase`, `kind` and the settle fields) that an update orchestration
took before it changed the cluster and released at the end
(`acquire_update_lock`, `renew_update_lock`, a settle hold, `release_update_lock`).
It was also the cluster's single "a deploy is under way" signal: the health
probe, the heartbeat's machine-offline alert, the log sink, the package refresh,
`ava stop`'s self-heal unpause, the roster and hosted admission all read it.

It closed three failures:

- **2026-06-01**: a manual rollout raced the cluster's own `ava.self.update` and
  both advanced the central schema.
- **2026-07-29**: a second fleet update started while hosts of the first were
  still converging and force-terminated two agents that had done nothing wrong
  (the settle hold), and the lease itself had lapsed mid-rollout because its TTL
  was the smallest of three independent bounds (`renew_update_lock`, the lattice
  ordering `NO_PROGRESS < LOCK_TTL`).
- **A hard-killed rollout** left a live-looking lease and a paused host behind
  (`ava cluster recover`).

The orchestrations that took the lease are gone. The last production caller of
`acquire_update_lock` was removed with the release path
([decision](2026-09-30-remove-release-image-path.md)), and no code has held a
settle hold since the old phase-based orchestration retired. The only production
writer left was `ava cluster recover`, which claimed the lease for 60 seconds in
order to clear a lease. Every reader therefore read "no lease": the roster's
`deploy_hold` was always null, the log sink never quieted a record, the package
refresh never skipped, the heartbeat never explained an offline runner, and the
deploy window's lease signal never fired. Reading a lease nothing writes made
those paths look guarded; they guarded nothing.

## Decision

Delete the lease and everything that only existed to serve it, without a
compatibility layer:

- `base.deploy.state.cluster_lock` (acquire, renew, release, the recovery claim,
  the lease read, `DeployLease`, the settle note) and its TTL clocks and lattice
  constraints, and the PITR activation lease renewer that was its last caller.
- Every lease reader: the deploy window's lease signal and the host updater
  lease, the roster's `deploy_hold` field and banner, the heartbeat's deploy
  explanation, the log sink's rollout quieting (and its per-process
  `deployment_state` read), the package refresh's in-flight skip, and the lease
  test in `ava stop`'s self-heal unpause.
- `ava cluster recover` with `cluster_recover_op`, `pause_owner.clear` and
  `force_clear`, the owner lock, and `ClusterUpdateInProgress`.
- `HostDeployState` stops reading `updater_lease_expires_at` and `paused_at`, and
  a posture write stops writing `paused_at`, so the columns are unused.
- `write_transaction(direct=True)`, whose only caller was the lease release.

`deployment_state.min_code_version` and its seed row stay: the code-version gate
reads and raises them. The deploy window keeps its posture signal (any machine's
`host_deploy_state` row not at `idle`); an operator-excluded machine's row is
ignored and logged whatever its posture. The database columns and tables are a
later migration.

## Why the failures no longer exist

- **Two updaters at once.** An update is one attended script,
  `python -m cli.fleet_update`, run by one operator
  ([decision](2026-09-30-networked-cluster-stays-on-source-updates.md)); it
  refuses a host that holds a maintenance hold and hosts whose HEADs disagree,
  and a rerun of a half is idempotent. There is no second automatic updater to
  exclude.
- **A second update over unconverged hosts.** Nothing takes a settle hold; the
  script waits for each unit's stop and start to return.
- **A stale writer after an update.** The
  [client-side code-version gate](../execution/converge/2026-09-30-client-side-code-version-gate.md)
  exits a process whose code is older than the cluster minimum at its next
  database borrow; no lease was ever what stopped it.
- **A lease that lapses mid-operation.** There is no operation that holds one.
- **A stranded pause.** The pause is the local maintenance journal, whose exits
  are `ava maintenance resume --cancel` and `repair`
  ([graceful maintenance](../../../../conventions/operations/graceful-maintenance.md)).

## Alternatives rejected

- **Keep `ava cluster recover`, cut down to an unpause and a journal clear.** Its
  forced clear of a journal it could not read is the opposite of the rule that a
  failed or unreadable ownership observation never permits a release. The
  journal's own exits cover every readable state; an unreadable one is removed by
  hand, an act the operator chooses.
- **Keep the read side "for a future writer".** A reader with no writer is the
  dead complexity [postmortem 0009](../../../../postmortems/0009-complexity-must-name-the-failure-it-prevents.md)
  names. A later coordination need starts from a design against that need.
- **Point the heartbeat's explanation at the posture row.** It would restore a
  suppression that has never worked, and is a behavior change that belongs in its
  own PR.

## Consequences

- No verb clears a stranded pause. Read it with `ava maintenance status`; end it
  with `resume --cancel` or `repair`. An unreadable journal, or a `paused` record
  with no hold, is removed by hand
  (`rm $AVA_HOME/run/deploy-pause-owner.json`) once no `ava stop` or `ava
  maintenance` command is in flight.
- The roster carries no deploy hold: `MachineStatus` (the roster at
  `/api/cluster/roster` and the cluster panel of `/api/status`) loses `deploy_hold`.
- The deploy window reads posture only. A non-idle row on a machine that is not
  excluded and is not coming back keeps it "in flight" until that host's
  `ava start` (or `ava cluster pause`, which excludes it).
- `db_pool_acquire_slow`, `db_outage_*` and the query-cancellation line keep the
  level they were logged at across an update, as they always did in production.
- A runner offline across an update is graded from its true start by the
  heartbeat, which never explained it. That gap is recorded, not fixed here.
- The lease and updater-lease columns of `deployment_state` and
  `host_deploy_state` stay in the schema, unread and unwritten, until the storage
  cleanup migration.

Forward link (2026-10-03): the stranded-pause exits are now `ava maintenance cancel` and
`repair`; `resume --cancel` was renamed. See
[the manual maintenance verbs deletion](../execution/converge/2026-10-03-delete-manual-maintenance-verbs.md).

Forward link (2026-10-04): `ava maintenance` is deleted entirely; the stranded-pause read is `ava
status` and the exit is `ava start`. See [delete-ava-maintenance](../../../agents/graph/2026-10-04-delete-ava-maintenance.md).
