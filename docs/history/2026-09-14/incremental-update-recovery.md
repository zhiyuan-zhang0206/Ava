# Incremental recovery evidence for updates

Migration-bearing updates must not repeatedly export an entire growing
database when a verified physical backup chain already exists. A quiet full
export can also outlast the rollout watchdog's silence budget while still
making progress, preventing the updater from delivering its own fix.

Use the existing drilled physical base and verify continuous, immutable,
offsite WAL through a fresh named restore point. Keep the check before
maintenance and bound its process below the watchdog deadline. An enabled
but broken chain refuses the update instead of silently selecting a costly
full-dump fallback. Disabled PITR retains the logical snapshot contract.

This deliberately reuses the base drill rather than replaying the entire
database during every update. The receipt records both that older drill and
the fresh archival evidence without calling the latter a completed restore
test. Physical recovery covers the whole instance and follows the existing
chain retention window. Logical backups and first activation's logical
recovery floor remain independent.

Do not bootstrap this change by editing production source, inventing a
completed backup, disabling the backup gate, or changing migration history.
The first deployment needs an independently verified transition through the
installed updater; a merged PR alone cannot change its imported code.
