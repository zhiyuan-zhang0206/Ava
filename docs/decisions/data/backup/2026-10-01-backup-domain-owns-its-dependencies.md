# The daily logical backup owns its naming, custody and off-site publish

## Context

The daily logical backup (`ava-pg-backup`: `pg_dump`, encrypt, off-site publish,
prune) reached into the physical PITR stack (`services/pitr`) for five things:
the managed-name grammar, the operation custody core that runs each scheduled
dump in an owned worker group, the off-site publish through a four-backend store
factory, the activation record that pinned a snapshot from pruning, and the
only operator verb that releases a blocked operation (`ava pitr operations`).
The stack is being replaced, and the daily backup is the one recovery path that
has to keep working through that replacement.

One edge made the coupling dangerous rather than untidy. The worker bootstrap
checks, in an isolated interpreter, that each package it names resolves inside the
controller's code root, and it named `services.pitr`. Deleting the stack would have
made every scheduled dump and restore drill exit at start: loudly (quarantine, an
error alert, a degraded `pg_backup`), but with the daily backup down until a fix
shipped.

## Decision

The backup domain owns what it uses, and the stack no longer sits under it:

- the managed-name grammar is `services/gateway_side/backup/names.py`, with only
  the kinds a writer still produces: `<db>-<UTC stamp>.dump.enc`;
- operation custody (`custody.py`, `worker_process.py`) lives in
  `services/backup_scheduler/operation/`, the bootstrap names that package, and
  its on-disk state (control and quarantine roots, kind names, record files,
  `.operation-*` directories) is unchanged, so a control directory a stopped
  controller left behind is read as before; `ava backup operations status|retire`
  replaces `ava pitr operations`;
- the off-site publish is `services/gateway_side/backup/offsite.py`: OSS only,
  one function, the same server-side guarantees (per-part `Content-MD5`, the
  multipart ETag chain, forbid-overwrite on completion only, adopt-after-crash)
  and the same visible acknowledgement; an unconfigured home skips with one INFO
  line;
- the activation pin, the in-process pre-activation snapshot, and the
  `pre-update`, `pitr-activation` and wall-clock-stamp name forms are deleted:
  nothing writes them.

The configuration keys (`AVA_PITR_STORE_BACKEND`, `AVA_PITR_OSS_*`) keep their
names in this change.

## Alternatives rejected

- **Leave a shim in `services/pitr` and delete the stack around it.** The shim
  would be the dependency this change removes, one rename away from the same trap.
- **Keep the store factory and its GCS, Baidu and COS adapters for the logical
  leg.** Only OSS publishes in practice; the other three adapters, the role
  protocols and the token machinery are most of the stack's store code, kept alive
  to serve one call.
- **Rename the configuration keys now.** The publish path has already failed
  silently once (a success that printed nothing was read as a dead upload). Changing
  its code and its configuration in one step makes a regression undiagnosable, and
  a rename needs an ordered unset-then-set across a restart. The rename is its own
  change.
- **Keep the old name forms readable.** Nothing writes them, and keeping them kept
  the pinned-snapshot and update-snapshot prune slots, and with them a read of the
  activation record from the backup path.
- **Keep writing the `.ack.json` sidecar.** Its only reader is the stack's
  retention inventory.

## Consequences

- A file in a retired name form that is still in a backup directory is no longer
  managed: it is not counted for due-ness and never pruned.
- Objects published earlier carry sidecars that nothing reads; they are harmless
  orphans, and re-publishing an existing name still adopts it by ETag chain.
- A backend other than `oss` no longer publishes off-site.
- Until the stack is deleted, its three operation kinds (base candidate, restore
  proof, operator drill) run on the moved core but have no retire verb.
- The configuration keys keep a PITR prefix until they are renamed.
