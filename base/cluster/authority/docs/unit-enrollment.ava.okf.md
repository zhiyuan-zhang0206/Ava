---
type: doc
title: Unit enrollment
description: The per-unit enrollment secret, its operator rotate/revoke commands, what a unit capability bundle exposes, and the coordinator channel's request proofs, replay window and sealing.
tags: [authority, lifecycle]
---

# Unit enrollment

The enrollment (`db-authority/units/<key>.json` on the gateway, the unit's copy
in `enrollment.json`) is the unit's durable identity toward a coordinator
channel; the human bearer and write generations never authenticate that
channel. It is minted when the unit first receives a bundle and reused by
later bundles. Only an operator command on the gateway changes it; the
commands print the enrollment id, never the secret:

- `ava cluster db-authority rotate-enrollment --machine M --home H`: new id and
  secret (atomic replace); the unit receives them with its next `issue-unit`
  bundle and fails channel authentication until then.
- `ava cluster db-authority revoke-enrollment --machine M --home H`: deletes
  the record; a later `issue-unit` re-enrolls the unit with a new secret.

Both change the coordinator channel only; neither touches the unit's
database login or API token (below).

## What a bundle exposes

An `issue-unit` bundle is sealed for one unit, but little of what it carries
is that unit's own:

| Carried | Shared by |
|---|---|
| enrollment secret | this unit alone (the coordinator channel key) |
| runner login (role and password) | every runner unit of the write generation |
| runner API token | every runner unit of the write generation |
| gateway API token digest | nobody: a digest admits nothing |
| telemetry token | every unit, until the human secret rotates |

A stolen bundle with its transport key, like a compromised unit, therefore
holds until the generation is revoked: the `ava_runner` database privileges;
a runner API token that the gateway API (a browser login included),
`/api/bootstrap` (the Redis runtime URL and provider keys) and every unit's
`/ops` of that generation admit; the OTLP relay ingress; and this unit's
coordinator identity. Its expiry is the installer's check, not the cipher's,
and nothing records its use: until it expires it installs on any unit that
names itself that machine and home.

The machine binding is a guard against mistakes, not against theft:
`install_bundle` compares the bundle's machine and home with the installing
unit's own first-start `--machine-name` and home, which that unit asserts
about itself, and the credentials work without any installation.

`revoke-enrollment` therefore does not contain a compromised unit or a lost
bundle. Rotate the write generation (the runbook's manual procedure revokes the
old generation's logins and tokens everywhere), then issue every unit a new
bundle, and revoke the lost unit's enrollment. What the bundle reached beyond
the generation rotates separately: the telemetry token with the human secret
(`scripts/data_plane_ops/rotate_cluster_secret.py`), the Redis runtime password bootstrap
served with `scripts/data_plane_ops/rotate_data_plane_secrets.py --scope runner`, and the
provider keys at each provider. No command rotates the write generation: the
procedure is [manual rotation after a credential leak](../../../../conventions/runbook.md#manual-rotation-after-a-credential-leak).

## Coordinator channel

`channel` is the coordinator channel's authentication (no listener uses it now): a request proof is an HMAC-SHA256 over the
protocol tag, operation, unit, enrollment id, method, path, body digest,
timestamp and nonce under an HKDF-derived key; the listener verifies it against
the gateway's current record (rotation and revocation take effect at once), a
per-operation `ReplayWindow` refuses a repeated nonce and a timestamp outside
300 s (a window does not survive a coordinator restart, so channel requests
are idempotent; the listener's handler threads share it, and it prunes,
checks and records a nonce under one lock, so concurrent copies of one
request admit once), and a forged proof never burns a nonce. `seal` / `open_sealed`
(AES-256-GCM under a key derived from the secret, operation and unit) carry a
payload for exactly one unit in one operation.
