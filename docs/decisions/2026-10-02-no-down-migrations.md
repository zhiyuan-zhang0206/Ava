# Migrations are forward-only, and a merged migration is immutable

## Context

Every post-baseline migration shipped a paired `.down.sql`, applied through
`rollback_to` / `apply_down`. Nothing in production ever called either: the update
path recovers by restoring a backup or fixing forward
([source updates](2026-09-30-networked-cluster-stays-on-source-updates.md)). The
down files still cost something on every migration: a second SQL file to write and
review, a lint for its `IF EXISTS` symmetry, and tests that executed them, none of
which prevented a failure that could happen.

The failure that did happen was the opposite one: a merged migration file was
deleted by a revert, and the develop and prod databases diverged — the applied set
named a migration the code no longer had, and the down that would have undone it
was gone too.

## Decision

- No down migrations. `rollback_to`, `apply_down`, `RollbackBelowFloor` and the
  `.down.sql` pairing lint are removed, along with the tests and docs that existed
  only for them; the existing `.down.sql` files are deleted. A `.down.sql` file is
  now a lint error.
- Expand-contract stays: the code that stops using an object ships first, and the
  drop is its own later migration.
- A new lint check, run in CI against the pull request's base revision, fails when
  a migration file already on main is modified, deleted or renamed. Only additions
  pass. A mistake in a merged migration is fixed by a new migration.

## Alternatives rejected

- **Keep the pairing, drop only the callers.** The files would stay as untested
  promises of a rollback no code path can run.
- **Store a checksum per applied migration in the database.** It detects an edited
  file at boot, after the damage is merged, and adds a column, a backfill and a
  failure mode for every existing database. The lint stops the change before it
  lands, at no runtime cost.

## Consequences

- Recovery from a bad migration is a backup restore (the WAL-G chain bounds how far
  back) or a forward fix; there is no per-migration undo.
- Folding history into the baseline deletes migration files, which the new check
  forbids; a future re-baseline edits the check in the same change.
