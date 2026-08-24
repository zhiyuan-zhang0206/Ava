# Atomic schema rollback

## Context

A failed recovery or cluster rollback used to preserve every successful down
migration before the failing one. The database was then neither at the
pre-rollback snapshot nor at a known revision, while resetting code would add a
second code/schema mismatch.

## Decision

`rollback_to` executes the complete down-migration batch in one transaction.
Each `apply_down` call remains independently safe when used alone and becomes a
savepoint within that batch. A failing down therefore restores every prior down
in the batch, including both schema objects and `schema_migrations` rows.

Recovery and cluster-rollback paths leave code on its current revision after
such a failure. The resulting schema is unchanged and compatible with that
revision, so a human can fix forward by re-running the update or start path,
or choose a different rollback target.

## Rationale

The transaction boundary turns an indeterminate partial rollback into a
determinate stopped-gateway state. It keeps the only automatic recovery action
safe: applying any pending migrations from the code that remains checked out.
