---
type: doc
title: Logical-backup passphrase
description: The one pinned passphrase every logical backup artifact is encrypted under, minted at birth, independent of the cluster secret, and backup-critical.
tags: []
---

# Logical-backup passphrase

Every logical backup artifact (`openssl enc -aes-256-cbc -pbkdf2`) is encrypted
under one passphrase: the file `$AVA_HOME/backups/logical-backup.passphrase`
(0600, 64 hex characters). `services/gateway_side/backup/passphrase.py` is its
only resolution, shared by every writer and restore path; it never derives a
key, so a home without the file refuses to back up or restore
([decision](../../../../decisions/2026-09-28-backup-passphrase-minted-at-birth.md)).

- **Birth**: a gateway claim mints and pins it (`ensure_minted`) before it
  publishes `.env`, whatever the cluster secret, so an empty-secret single box
  encrypts for real.
- **Homes born earlier** carry the passphrase they had encrypted under,
  `sha256(secret)`, pinned once at their conversion. An empty secret's
  derivation is the public `LEGACY_EMPTY_SECRET_PASSPHRASE`, so such a home
  carries a minted passphrase; its artifacts from before the pin restore only
  with the explicit `scripts/data_plane_ops/restore_drill.py --legacy-empty-secret-passphrase`,
  which is never tried as a fallback. A failed decryption names that option.
- **Secret rotation** (`scripts/data_plane_ops/rotate_cluster_secret.py`) never touches it.

## Escrow

Losing the file makes every logical backup of the home unreadable, local and
off-site alike; nothing can re-derive it. Export it once after birth and keep
the copy with the gateway's other backup keys, off the gateway host:

```bash
install -m 600 "$AVA_HOME/backups/logical-backup.passphrase" /secure/escrow/<home>-logical-backup.passphrase
```

To restore on a rebuilt gateway, put the escrowed file back at the same path
(0600, owned by the gateway's OS user) before the first backup or restore.

Parent: [[backup.ava.okf.md|PG-Backup]].
