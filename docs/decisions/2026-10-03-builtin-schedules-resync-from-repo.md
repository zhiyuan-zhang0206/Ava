# Built-in schedules follow the repo template

## Context

The 2026-08-11 pre-open-source ruling (recorded in `schedules/manifest.json`, `schedules/README.md` and
the `builtin_schedules` docstring, not in a decision file) made provisioning create missing built-in
schedules and never touch an existing row, so an operator's edits survived every boot. The row's
`script` is a snapshot of the template taken when the row was created.

On 2026-10-03 `catch_up()` and `fire_slot_once()` gained a required `db` and the hierarchy-worker's
`prepare` moved. No stored snapshot followed, and 13 schedules crash-looped after the update. The
import-only `ava schedules verify` stayed green because every imported name still existed.

## Decision

- **Provisioning resyncs `script` and `command`** of a manifest-named row whose content hash differs from
  the template (schedule-manager start, before its first reconcile; `ava schedules provision`). It
  writes a `schedule_versions` row (`builtin-resync <hash>`, so the replaced text stays recoverable) and
  the same `schedule_sync_requests` upsert an API script edit leaves, so a live session relaunches.
  `enabled`, `description` and status of an existing row are still never touched. Rows that are not in
  the manifest (agent-created) are never read or written.
- **`ava schedules verify` binds call sites** with `inspect.signature`, so a stored script that no
  longer matches the library is red before it fires. This is the only guard for agent-created scripts,
  and `fleet_update down` runs it with the new checkout's code before stopping anything.
- **Converge replaces a differing `ava-ops-main.json`** instead of preserving it behind the user-edit
  hash guard. `ava lgtm render --force` writes the file without recording its hash, so every later
  rollout read it as a local edit and kept it stale.

This follows the 2026-10-03 rule that local copies of repo-derived content converge to the source
([core-package-update-channel](../../future/infra/core-package-update-channel.md): a differing copy is
replaced and reported, the old tree is kept): a built-in's stored script and the generated dashboard
are derived state, and the user does not hand-edit them.

## Alternatives rejected

- **The runner reads the repo file for built-ins.** It forks the runner's `_load` on "is this row
  built-in", and leaves `get`, the UI, `verify` and `update` showing a script that is not the one that
  runs.
- **Keep never-touch and document `ava schedules update` per template change.** That is the procedure
  that failed: it depends on someone remembering, once per changed built-in, per cluster.
- **Keep the dashboard's user-edit guard and add a hash write to `render --force`.** It fixes new
  writes but not hosts already stuck, and the file has no hand-edit contract to protect.

## Consequences

- A hand edit of a built-in's stored script is replaced at the next provision (recoverable from
  `schedule_versions`); change the template instead.
- A hand edit of the rendered dashboard is replaced at the next converge.
