---
type: doc
title: Write-generation delivery and wiring
description: How the active write generation reaches the pooler userlist, launched services, admitted operator processes and remote agent-runner units, and the birth, ordinary-start and cutover call sequences.
tags: [postgres, authority, lifecycle]
---

# Write-generation delivery and wiring

## Delivery

`delivery` hands the active generation to its three consumers, each bound to
the ledger's credential digest:

- `render_userlist`: the pooler's `auth_file` — the generation's two SCRAM
  verifiers plus the admin-console entry `ava_pooler_admin`
  (`pooler-admin.json`, created once by birth or cutover, never a PostgreSQL
  role). Deterministic bytes, so an unchanged userlist is never restarted.
- `write_grant`: one class login of the ACTIVE generation (a pending or revoked
  one is never delivered) for a launch environment; `reference` is the only
  part a launch digest or journal carries.
- `consume`: the gateway login for an operator process the launcher did not
  inject, only after `require_admitted_runtime` (the selected release image's
  prefix, or the start intent's source checkout).

## Remote agent-runner units

Bootstrap (`GET /api/bootstrap`) serves the credential-free endpoint and no
login, and a runner strips any password an older gateway still serves
(`shared.bootstrap.fetch_bootstrap_config`). `unit` is the only path that
carries a login to another home:

- **Issue** (gateway, `ava cluster db-authority issue-unit --machine M --home H
  --out FILE`): `issue_bundle` seals the ACTIVE generation's runner login, the
  endpoint bootstrap serves (`service_read.served_db_endpoint`), the unit's
  enrollment and a binding (machine, home, generation number and credential
  digest, nonce, expiry) with AES-256-GCM under a fresh 32-byte transport key.
  The key is printed once and stored nowhere; the header (machine, home,
  generation, expiry, nonce) is readable and authenticated as associated data.
  The enrollment is minted once per unit into `db-authority/units/<key>.json`
  and reused by later bundles; deleting that record revokes it. Refused on a
  pure runner, a remote-managed plane (no generation exists there), a home
  without an active generation, and while a release operation is incomplete.
- **Install** (unit, `ava start --db-capability FILE`, key in
  `AVA_DB_CAPABILITY_KEY`, popped at once): `open_bundle` refuses anything that
  fails authentication or has expired; `install_bundle` requires this machine
  name and home, the endpoint the gateway serves now, a generation not older
  than the installed one (an equal number must carry the same credential
  digest), and a login the cluster accepts (`SELECT 1` through the endpoint, so
  a revoked generation never installs). It writes `unit.json` and
  `enrollment.json` (0600) and start deletes the bundle. A first join with no
  bundle and no installed capability refuses before identity is persisted.
- **Deliver**: the unit's root launcher gives every runner-class service
  `unit_delivery` (the login on the recorded endpoint plus
  `AVA_DB_GENERATION`; a gateway-class service on a pure runner fails the
  launch); the launch digest binds `unit_reference`. The boot pass keeps an
  environment login only when it equals the installed delivery exactly
  (`is_delivered_login`), including across the bootstrap injection; an operator
  process consumes it (`consume_unit`) only while it runs the home's admitted
  runtime; anything else records a refusal naming the issue command, raised at
  the first dial.

A new generation reaches a remote unit only through a new bundle (the one-time
cutover, a join, an emergency). Networked release operations keep refusing
until the fleet transition exchanges capabilities automatically.

## Unit enrollment

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

## Wiring

- **Birth** (`cli/commands/_data_plane.complete_gateway_data_plane`, start
  intent `configured`): `ensure_groups` -> `ensure_monitor` ->
  `retire_legacy_logins(Birth)` ->
  `create_ledger` -> `ensure_pooler_admin` -> `mint_generation` -> pooler
  serving the pending pair -> a pooled `SELECT 1` as each login -> `activate`.
  A retry reconciles to the same generation 0.
- **Ordinary start**: `ensure_groups` after migrations -> `ensure_monitor` ->
  `sweep` -> `check_invariant` (read-only grantees such as `grafana_ro`); a home
  with no ledger is refused before any native effect.
- **Launch**: the root launcher delivers `AVA_DB_URL` + `AVA_DB_GENERATION` per
  service class; `shared/dotenv_boot` keeps a delivery naming this home's
  endpoint and consumes the gateway login for an admitted operator process;
  otherwise the first dial raises `NoDatabaseAuthorityError`.
- **Cutover** (`scripts/cutover_db_authority.py`, step `db`):
  `retire_legacy_logins(Cutover)` -> `ensure_groups` -> `ensure_monitor` ->
  `prove_closure` over the
  legacy roles -> ledger -> generation 0 -> pooler -> proof -> `activate` ->
  invariant -> credential-free `.env`. Step `remote-units` (networked homes):
  explicit classification of every other `machine_units` row -> Redis admin
  rotation -> one `issue_bundle` per included unit.
- **Monitoring** is not delivered: the collector's PostgreSQL receiver
  (`cli/commands/_otel_collector.py`) dials the owner-only socket as
  `ava_monitor` by `peer`, so its rendered config names no credential and a
  rollout leaves it working.
- **Release** (`cli/release_transition/authority.py`, under the home operation
  lock, with `OperationAuthority(operation, direction)`): preflight
  `check_invariant` -> `fencing`: `revoke` -> owned pooler stopped (escalating;
  no listener left) -> `close_revoked` -> `prune` -> `authorizing`:
  `mint_generation` -> pooler serving the pending pair -> a pooled `SELECT 1`
  as each login -> `activate`; observation re-checks the invariant and that
  `stale_sessions` is empty. The journal records each step's intent and
  receipt ([[cli/release_transition/execution.ava.okf.md|release execution]]).
