# Scheduled backups retire proven-closed failures automatically

## Context

Each PITR/backup operation is one directly owned worker process group. A failed
or cancelled operation keeps its control files, staged artifact and partial
work as evidence, and the next operation refuses to start while any earlier
`.operation-*` control directory exists. There is no command to retire it.

The common trigger is routine: `ava stop` or a release drain during the nightly
dump. Backup coverage then stops silently until someone intervenes. An
interrupted off-site upload also strands a complete dump inside the controls,
and a failed in-process snapshot leaves a plaintext `.dump.partial` that nothing
removes.

## Decision

User ruling, 2026-09-26. Distinguish failures by custody evidence:

- **Closure proven** (the direct owner confirmed the worker group closed and
  reaped the leader, including cancellation by stop/drain): move the operation's
  controls, staged artifact and partial work into a quarantine archive, raise an
  alert, and let the next scheduled run proceed.
- **Closure not proven** (unresolved custody, controller death, uncertain group
  closure): keep blocking the schedule, raise an alert, and require an explicit
  operator `retire` command that re-checks closure before releasing it.

Plaintext partial dumps are removed once their writer's closure is proven; they
are never kept in the backup directory as an unencrypted artifact.

## Alternatives rejected

- **Block every failure until manual retirement.** The most conservative, but
  each release that overlaps the nightly dump would halt backups and require
  manual work. Proven closure already establishes that nothing can still write,
  which is exactly what the block protects against.
- **Delete failed operation evidence automatically.** Loses the only record of
  what happened; quarantine keeps it without blocking.

## Consequences

- The quarantine archive needs retention and a size bound, and must not hold
  plaintext dump material.
- A completed dump stranded by an interrupted upload should be recoverable from
  quarantine rather than rebuilt; whether it is re-published automatically is
  an implementation choice to settle with the closure evidence.
- Controller death remains the case that needs a human, until a platform owner
  (for example a Linux cgroup) can prove closure without the original controller.
