# Pre-update snapshots keep their own retention slot

## Context

`ava cluster update` writes a verified `<db>-<ts>.dump.gz.enc` data snapshot
before applying migrations whenever the target's migration set differs from the
applied set (b14074efa). Snapshots land in the same pool as the daily dumps and
share their name pattern, so prune's `BACKUP_KEEP = 7` counted them as dumps:
a week with several updates silently shrank the daily window, and nothing
guaranteed a pre-migration restore point survived.

## Decision

Pre-update snapshots carry a kind segment in the name —
`<db>-<ts>.pre-update.dump.gz.enc` — and prune keeps the newest `BACKUP_KEEP`
**daily** dumps plus the newest **one** pre-update snapshot. Local and Drive
prune share the same function, so the off-site lifecycle matches. `is_due`
still counts a snapshot as today's dump (a same-day pre-03:00 update suppresses
that day's daily run, which is acceptable: the snapshot is minutes older than
the daily would have been, and it saves ~1 GB of disk per such day).

## Alternatives rejected

- **Count snapshots into the 7-slot budget (status quo).** Updates silently
  erode the daily history and can prune every pre-migration restore point.
- **A separate directory for snapshots.** Larger surgery (due-check, off-site
  scope, restore drill all branch on location) for the same classification a
  name segment gives.

## Consequences

- A week's daily history is guaranteed regardless of update frequency; the
  newest snapshot always survives (it is the most recent full dump before a
  migration).
- Restore drill and `_managed_dumps` work unchanged: the kind segment still
  matches `_NAME_RE`, and the newest managed artifact is still the newest dump.
- Older snapshots are pruned like dailies, so the pool grows by at most one
  artifact beyond the daily window.
