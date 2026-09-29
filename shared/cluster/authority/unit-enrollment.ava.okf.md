---
type: doc
title: Unit enrollment
description: The per-unit enrollment secret a release coordinator authenticates, its operator rotate/revoke commands, what a unit capability bundle exposes, and the coordinator channel's request proofs, replay window and sealing.
tags: [authority, lifecycle]
---

# Unit enrollment

The enrollment (`db-authority/units/<key>.json` on the gateway, the unit's copy
in `enrollment.json`) is the unit's durable identity toward a release
coordinator; the human bearer and write generations never authenticate that
channel. It is minted when the unit first receives a bundle and reused by
later bundles. Only an
operator command on the gateway changes it, refused while a release operation
is incomplete; the commands print the enrollment id, never the secret:

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
bundle. Rotate the write generation (a release transition revokes the old
generation's logins and tokens everywhere), then issue every unit a new
bundle, and revoke the lost unit's enrollment. What the bundle reached beyond
the generation rotates separately: the telemetry token with the human secret
(`scripts/rotate_cluster_secret.py`), the Redis runtime password bootstrap
served with `scripts/rotate_data_plane_secrets.py --scope runner`, and the
provider keys at each provider. A networked home cannot run a release
transition yet (`NETWORKED_REFUSAL`, slices dbgen-8 and FC-9), so until those
land it has no in-band way to rotate its write generation.

## Coordinator channel

`channel` is the coordinator channel's authentication (the listener itself
belongs to the fleet transition): a request proof is an HMAC-SHA256 over the
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

## Before remote units (dbgen-8/FC-9)

The coordinator channel carries no remote unit yet (`require_topology`
refuses them). These must land before it does:

- **Authenticated coordinator responses.** The channel MACs requests only:
  the unit's client parses any `200` body as an `Instruction`, over plain
  HTTP to the captured endpoint. An on-path attacker, or anything that
  answers at that endpoint, can order a unit to `close`, `excluded` or
  `restore`, and the follower complies, because it checks only the
  operation, unit, image and maintenance hold: a capability is sealed, an
  order is not. The plan: the listener MACs every response under its own
  HKDF label (`response`) over the protocol, operation, unit, the request's
  nonce, the status and the body's SHA-256, and the client verifies that MAC
  before it parses anything, refusing a response without one. Covering the
  request's nonce binds each answer to the one request it answers, so a
  captured answer cannot be replayed to a later request. The protocol tag
  (`ava-coordinator/1`) is not frozen into any shipped image yet, so the
  change needs no second release.
