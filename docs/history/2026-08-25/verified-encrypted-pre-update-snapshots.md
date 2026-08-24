# Verified encrypted pre-update snapshots

## Context

Migration-bearing gateway updates need a data recovery point before the gateway
stops. The daily backup pipeline now publishes encrypted, gzip-compressed
artifacts and runs from its own scheduler, so a direct `pg_restore --list` of
the published path is no longer meaningful and a scheduler run can race an
update snapshot.

## Decision

Pre-update snapshots use the normal encrypted backup pipeline with a tighter
20-minute creation limit. Backup creation remains serialized by one re-entrant
cross-process lock, which is held through artifact verification. The verifier
decrypts, unzips, and lists the temporary custom dump; only a non-empty table of
contents permits the rollout to proceed.

## Rationale

Using the standard pipeline preserves private local storage, password-free
command arguments, checkpoint inclusion, retention, and the optional off-site
copy. Keeping verification inside the same lock ensures the exact artifact
handed to recovery was proven restorable, rather than a same-second scheduler
replacement or an unverified encrypted blob.
