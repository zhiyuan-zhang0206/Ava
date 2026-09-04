---
type: doc
title: Prepared unit inventory
description: 'Verified-image producer binds the retained service roster plus residual session/launcher facts into an immutable receipt for managed-writer observation; read-only, no maintenance permission.'
read_when: Connecting verified release preparation to managed-writer observation.
tags:
- runtime
- release
---

# Prepared unit inventory

`_release_inventory.prepare_unit_inventory` runs from the verified installed
image with an explicit canonical registered unit home. It reads the existing
annotated service roster (including presence-discovered plugin services), every
home session record, and user launchd/crontab definitions. Disabled and obsolete
session names remain in the inventory. Missing, malformed, unsupported and
changing facts refuse before writing a receipt.
Session/definition reads are bounded and verify the opened inode against the
path observation; replacement or growth during a read refuses the inventory.

Native reads reuse `shared.native_job_observation`: launchd enumeration requires
the current user's proven Aqua domain and two identical label snapshots. Raw
plist bytes must match the label-addressed native reader before hashing. Cron
uses the same bounded reader as observation. An unavailable domain is an error,
never an empty inventory or positive shutdown result.

The existing `ExpectedUnitWriters` model carries exact process/session/job
identities. The full secret-free prepare receipt also carries the complete
service roster and its gates. Its filename is the canonical payload SHA-256;
later adoption must bind this **whole receipt digest**, not only the narrower
`expected.unit().inventory_digest`. Image manifests cannot embed this receipt:
it already references their final digest. The receipt is outside the image,
under the existing unit run directory, and is distinct from post-stop candidate
collection. `revalidate_prepared_inventory` rereads actual sources and rejects
omissions and changed unit/service/session/job facts.

Restricted bootstrap resume uses the same real producer through
`revalidate_bootstrap_inventory`. It requires the complete receipt's static
unit, service-roster, launcher, and unresolved-coverage facts to remain exact.
Only the sole `ava-ops` process/session identity may differ, after the updater
has independently proved its native record, command, liveness, and verified A/B
image. This exception is not available to ordinary preparation or maintenance.

This bounded producer does **not** cover non-session managed processes,
predecessor orchestration, system/alternate-user jobs, or positive launcher
shutdown. Those gaps are explicit in the receipt and the maintenance consumer
unconditionally refuses in this version. Removing a JSON flag cannot grant
permission. The actual observer consumes the expected model and reports unknown
closure; no new registry, controller or readiness claim is introduced.

CI prepares a real wheel, removes the source checkout, uses native PostgreSQL
unit registration and real process/session records, and consumes the receipt
through the actual observer. Scheduler command responses are controlled
read-only fixtures, not claims of native launchd/cron shutdown. Negative checks
cover residual session omission, old launcher drift, plugin service omission and
changed unit registration. Full image bytes remain unchanged after the proof.
