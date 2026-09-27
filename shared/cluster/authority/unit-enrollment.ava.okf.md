---
type: doc
title: Unit enrollment
description: The per-unit enrollment secret a release coordinator authenticates, its operator rotate/revoke commands, and the coordinator channel's request proofs, replay window and sealing.
tags: [authority, lifecycle]
---

# Unit enrollment

The enrollment (`db-authority/units/<key>.json` on the gateway, the unit's copy
in `enrollment.json`) is the unit's durable identity toward a release
coordinator; the human bearer and write generations never authenticate that
channel. It is minted when the unit first receives a bundle — its join or the
one-time cutover (`remote-units` step) — and reused by later bundles. Only an
operator command on the gateway changes it, refused while a release operation
is incomplete; the commands print the enrollment id, never the secret:

- `ava cluster db-authority rotate-enrollment --machine M --home H`: new id and
  secret (atomic replace); the unit receives them with its next `issue-unit`
  bundle and fails channel authentication until then.
- `ava cluster db-authority revoke-enrollment --machine M --home H`: deletes
  the record; a later `issue-unit` re-enrolls the unit with a new secret.

`channel` is the coordinator channel's authentication (the listener itself
belongs to the fleet transition): a request proof is an HMAC-SHA256 over the
protocol tag, operation, unit, enrollment id, method, path, body digest,
timestamp and nonce under an HKDF-derived key; the listener verifies it against
the gateway's current record (rotation and revocation take effect at once), a
per-operation `ReplayWindow` refuses a repeated nonce and a timestamp outside
300 s (a window does not survive a coordinator restart, so channel requests
are idempotent), and a forged proof never burns a nonce. `seal` / `open_sealed`
(AES-256-GCM under a key derived from the secret, operation and unit) carry a
payload for exactly one unit in one operation.
