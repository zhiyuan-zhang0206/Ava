# The managed-writer publication fence is removed

## Context

The retained-image release path needed a fence around hosted runtime births.
An image rollout replaces the code of every unit in steps, so a runtime could
be born between the old writer stopping and the new image being recorded as
current, and a birth under the wrong code would write to the shared database
with the wrong protocol. The fence was a JSONB record in
`deployment_state.managed_writer_evidence` (a `current` all-unit publication and
a `pending` one), a typed collection barrier that proved the old writers were
gone (`base/deploy/writers/barrier.py`), and a runtime-admission decision that
every hosted birth ran under a row lock on `deployment_state`:

- **Pending, or a non-stable phase under a live deploy lease:** the birth was
  deferred and no agent row was touched.
- **Current:** protocol v1 was advertised for the row.
- **No record (NULL):** the row stayed at protocol zero, exactly as before.

[The release/image path was removed](2026-09-30-remove-release-image-path.md).
No production code writes the record any more, so admission takes the NULL
branch. The only remaining way to defer a birth was the 60-second
lease that `ava cluster recover` claims, and that command exists only to clear
a lease the removed rollouts left behind. What the fence still cost was a
`FOR UPDATE` on the single `deployment_state` row in every hosted admission on
every machine, a cross-machine round trip and a cluster-wide serialization
point, to reach a decision that could not change.

The failure the fence prevented was a birth under stale code while writers are
being replaced. An update is now `python -m cli.fleet_update`: it stops every
unit before it switches any checkout, and a process that missed the stop is
ended by the
[client-side code-version gate](2026-09-30-client-side-code-version-gate.md) the
next time it borrows a connection. No update runs while a unit is serving, so
there is no replacement window in which a birth needs fencing.

## Decision

Delete the publication package `base/deploy/writers/` (the publication envelope
and its begin, adopt and require helpers, the barrier and the runtime-admission
decision), its tests and the workflow that proved it. Hosted admission decides
on the agent's own row, its resource evidence and the maintenance hold, and
writes `runtime_protocol_version = 0` as a literal. It reads and locks nothing
deployment-wide.

The resource fence stays where it was, inline in `admit_hosted_runtime`: a stored
resource value the current model cannot decode refuses the birth as
`resource_fence`. The maintenance hold keeps its own gates.

The schema is not touched. `deployment_state.managed_writer_evidence`, the
`lock_runtime_publication_admission()` function and the `publication_deferred`
admission outcome stay until a later migration drops them, after this code has
run in production, so any single update stays reversible.

## Alternatives rejected

- **Keep it shelved.** The dead code costs every admission change a second
  implementation to keep compiling and testing, and the row lock costs every
  hosted birth a round trip and a serialization point on a row nobody
  contends for. Postmortem 0009 describes this shape: complexity that names no
  failure it still prevents.
- **Activate protocol v1 instead.** Advertising v1 from hosted admission is a
  product behavior change with its own gate and tests, not a cleanup. The
  caller gate (`require_caller_protocol`, the MCP `caller_protocol` argument)
  and the `agents_meta.runtime_protocol_version` column keep their present
  behavior: admission writes zero, and the caller gate refuses v1 callers.
  Whether v1 is activated or retired is a separate decision.
- **Drop the column, the function and the outcome value in the same change.** A
  contraction migration needs the code that stops using the objects to have
  shipped first, so that a rollback of the migration lands on code that never
  reads them. The deploy-lease guards in `cluster_lock.py` also still read the
  column until the lease itself is removed.

## Consequences

- A hosted admission no longer takes a lock on `deployment_state` and is no
  longer deferred by a non-stable deployment phase under a live lease. The
  drain continuation that the deferral once needed an exemption for (issue
  #2159) has nothing to be exempt from.
- Every hosted admission advertises protocol zero, as it did in production
  already, and MCP v1 callers keep being refused with the refusal that names
  the unmet condition.
- The stored `managed_writer_evidence` value, whatever it holds, is inert to
  admission. The `pending` guard on the deploy lease in `cluster_lock.py` still
  reads it and is removed with that lease.
- A stored resource value the current model cannot decode still refuses the
  birth as `resource_fence`, carrying the shape error's message.
