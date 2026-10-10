# Commit termination before best-effort PTY cleanup

## Decision

The user chose short lifecycle transactions and best-effort PTY cleanup on
2026-10-10. This superseded the under-lock cleanup timing and crash guarantee in
[the 2026-09-27 termination decision](../../agents/lifecycle/2026-09-27-terminate-has-no-closed-state.md).
Ordinary termination without `kill_all_shell_sessions` was unchanged.

The existing `agents_meta.session_index` was sufficient to bound cleanup. Shell
and page allocation incremented this monotonic counter under the same agent row
lock, returned the previous value as the session ID, and never reused IDs. A
cutoff captured under the lifecycle lock therefore excluded later allocations.
No new schema, generation identity, cleanup lease or durable cleanup state was
needed.

Graceful apply retained its exact incarnation, command, lease and resource
closure checks. When cleanup was requested, it captured the next session ID,
committed termination and its observed command, then attempted listed shell IDs
strictly below that cutoff outside the transaction. An already-terminated
request captured its cutoff in the existing locked enqueue transaction.

Force acceptance captured K1 and attempted older sessions after the force fence
committed. The original host's existing quiescent settlement captured one fresh
K2 after validating the exact generation, owner and command and proving owned
resources closed. It committed observation before attempting IDs below K2. Boot
recovery used the same ordering after its existing process exclusivity, resource
evidence and target checks. K2 covered allocations made while the old step
drained after K1; neither path refreshed its cutoff or repeated enumeration.

## Accepted consequences

PTY cleanup did not establish execution-child resource closure. Those proofs and
their admission fences stayed unchanged. The backend still checked actual
process identity before signaling; page sessions were excluded, and disappeared
sessions remained idempotent successes.

Cleanup failure could not roll back termination or command observation. The
synchronous op propagated failure instead of returning a successful killed-ID
list. The host reported the original exception at ERROR while keeping the
termination committed. A missing configured killer was still rejected before
committing the lifecycle transition.

Graceful apply attempted cleanup even when its post-commit notification failed,
preserving that publication exception and reporting a secondary cleanup failure.
Force acceptance could still fail during its post-commit publication before the
immediate K1 attempt. The durable request remained for the original host's K2
attempt after resource settlement; this was not a guarantee that a stalled or
crashed host would ever reach cleanup.

A crash between commit and cleanup, an allocated session not yet visible during
enumeration, or old work allocating after the final cutoff could leave a live
PTY. These leftovers were explicitly accepted for operator cleanup. There was
no automatic retry receipt or scan. Sessions allocated at or above a captured
cutoff were spared even if a replacement started before cleanup completed. A
replacement reusing an older persistent session could still observe that session
being closed: the cutoff identified allocation age, not runtime membership.

Keeping the previous guarantee would require holding the row lock across slow
external cleanup or adding persistent cleanup coordination. Neither cost was
justified for this best-effort option.
