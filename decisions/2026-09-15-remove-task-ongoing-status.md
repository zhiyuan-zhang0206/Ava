# Remove the 'ongoing' task status

## Context

The 2026-08-27 ruling pinned the system root task to `ongoing` and reserved the
graph's violet for it; a 2026-09-01 allowance then let regular domain-anchor
resident tasks use `ongoing` as well ([superseded decision](2026-09-02-ongoing-domain-anchor-tasks.md)).
In practice the vocabulary was misread at both ends: the user had been using
"ongoing" as a synonym for in_progress, and the literal implementation produced a
fourth status whose zh catalog label was the same string as `in_progress`'s,
whose legend entry duplicated the in-progress one, and whose root pin made the
root's state read as its own thing. User feedback 2026-09-15: the task system has no
`ongoing` state at all — the root is simply permanently `in_progress`.

## Decision

Remove `"ongoing"` entirely (user ruling 2026-09-15):

- Task statuses are exactly `in_progress` / `done` / `cancelled`: the DB CHECK
  narrows, and the SDK, gateway wire schemas, OpenAPI, generated frontend types,
  i18n catalogs, docs, and OKF nodes all drop the value.
- `shared/task_status.py::TaskStatus` is the one source of the set.
  `tests/test_db_check_enum_sync.py` locks db/schema.sql to it; a wire-format
  test locks the generated OpenAPI enum and the TS mirror; the frontend legend
  and render sets are asserted in the component tests.
- The root's specialness is modeled explicitly instead of as a status: it is
  pinned to `in_progress` (`agent_tasks_root_status_in_progress` CHECK),
  immutable, and rendered violet with a "Root" legend entry. The graph legend
  lists only the categories the canvas actually renders (in_progress + root) —
  done/cancelled get no legend line.
- The 2026-09-01 allowance, its SDK ownership gate, and its
  "active = in_progress or ongoing" spillovers (machine pause drain, plugin
  inspector, terminate hint) revert to the plain `in_progress` predicate.

## Alternatives rejected

- **Keep `ongoing` root-only (the 2026-08-27 state):** the root still would not
  be `in_progress`, which the user's model says it is; and the state remains a
  misleading near-synonym of `in_progress` everywhere it surfaces.
- **Keep `ongoing` for long-running regular tasks (the 2026-09-01 state):** the
  ruling is that the state does not exist — the duplicated label and legend
  entry were symptoms, not the disease.

## Consequences

- Every `ongoing` row migrates back to `in_progress`
  (`20260915T064420_drop-task-ongoing-status`); a rollback restores the old
  constraints and the root's `ongoing` pin but does not guess regular rows back
  (nothing distinguishes a migrated row from one born `in_progress`).
- Long-running work loses its reminder exemption: reminders cannot be disabled,
  so a resident task that must not nag is either closed between runs or accepts
  its reminder cadence.
- The two migrations that established the `ongoing` semantics are stamped as
  folded in `db/schema.sql` (the fresh baseline pins `in_progress`), so fresh
  DBs do not replay them.
