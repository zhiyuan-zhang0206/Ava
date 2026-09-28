---
type: doc
title: Write-generation delivery and wiring
description: How the active write generation reaches the pooler userlist, launched services, admitted operator processes and remote agent-runner units (login and API token), and the birth, ordinary-start and cutover call sequences.
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
- `write_grant`: one class login and API token of the ACTIVE generation (a
  pending or revoked one is never delivered) for a launch environment;
  `reference` is the only part a launch digest or journal carries.
- `consume`: the gateway login (and, while the API is authenticated, the
  gateway API token) for an operator process the launcher did not inject, only
  after `require_admitted_runtime` (the selected release image's prefix, or the
  start intent's source checkout); `operator_environment` is that delivery as
  environment, refused for a launcher-started process.

## Machine API tokens

Each generation's API tokens gate the HTTP API and `/ops`:
[[shared/cluster/authority/api-tokens.ava.okf.md|Machine API tokens]].

## Remote agent-runner units

Bootstrap (`GET /api/bootstrap`) serves the credential-free endpoint and no
login, and a runner strips any password an older gateway still serves
(`shared.bootstrap.fetch_bootstrap_config`). `unit` is the only path that
carries a login to another home:

- **Issue** (gateway, `ava cluster db-authority issue-unit --machine M --home H
  --out FILE`): `issue_bundle` seals the ACTIVE generation's runner login, the
  unit's API admission (`UnitApi`: the runner API token, the gateway token's
  digest and the telemetry token; none when the gateway's API is open), the
  endpoint bootstrap serves (`service_read.served_db_endpoint`), the unit's
  enrollment and a binding (machine, home, generation number and credential
  digest, nonce, expiry) with AES-256-GCM under a fresh 32-byte transport key.
  The key is printed once and stored nowhere; the header (machine, home,
  generation, expiry, nonce) is readable and authenticated as associated data.
  The enrollment is minted once per unit into `db-authority/units/<key>.json`
  and reused by later bundles; deleting that record revokes it. Refused on a
  pure runner, a remote-managed plane (no generation exists there), a home
  without an active generation, and while a release operation is incomplete.
  Only the enrollment is the unit's own; the login and tokens are shared
  ([what a bundle exposes](unit-enrollment.ava.okf.md#what-a-bundle-exposes)).
- **Install** (unit, `ava start --db-capability FILE`, key in
  `AVA_DB_CAPABILITY_KEY`, popped at once): `open_bundle` refuses anything that
  fails authentication or has expired; the join's bootstrap fetch presents the
  bundle's API token (never the human secret); `install_bundle` requires this machine
  name and home, the endpoint the gateway serves now, a generation not older
  than the installed one (an equal number must carry the same credential
  digest), and a login the cluster accepts (`SELECT 1` through the endpoint, so
  a revoked generation never installs). It writes `unit.json` and
  `enrollment.json` (0600) and start deletes the bundle. A first join with no
  bundle and no installed capability refuses before identity is persisted.
- **Deliver**: the unit's root launcher gives every runner-class service
  `unit_delivery` (the login on the recorded endpoint plus
  `AVA_DB_GENERATION`) and `unit_api_delivery` (its API token; a gateway-class
  service on a pure runner fails the launch); the launch digest binds
  `unit_reference`. The boot pass keeps an
  environment login only when it equals the installed delivery exactly
  (`is_delivered_login`), including across the bootstrap injection; an operator
  process consumes it (`consume_unit`) only while it runs the home's admitted
  runtime; anything else records a refusal naming the issue command, raised at
  the first dial.

A new generation reaches a remote unit only through a new bundle (the one-time
cutover, a join, an emergency). Networked release operations keep refusing
until the fleet transition exchanges capabilities automatically.

## Unit enrollment

A unit's durable identity toward a release coordinator, its operator commands
and the coordinator channel's authentication:
[unit enrollment](unit-enrollment.ava.okf.md).

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
  otherwise the first dial raises `NoDatabaseAuthorityError`. The release
  handoff (`ava cluster update --prepared`) builds no Settings: it takes the
  same operator login itself and hands it, in the exec environment only, to
  the executor image's submission, which is not admitted until selected
  ([[cli/release_handoff/release_handoff.ava.okf.md]]).
- **Cutover** (`scripts/cutover_db_authority.py`, step `db`):
  `retire_legacy_logins(Cutover)` -> `ensure_groups` -> `ensure_monitor` ->
  `prove_closure` over the
  legacy roles -> ledger -> generation 0 -> pooler -> proof -> `activate` ->
  invariant -> credential-free `.env`. Step `api`: pin the logical-backup
  passphrase (`sha256(secret)`, or a minted one for an empty secret); on a
  networked home inside the one human-bearer rotation
  (`scripts/rotate_cluster_secret.advance`, journaled as fingerprints). Step
  `remote-units` (networked homes): explicit classification of every other
  `machine_units` row -> Redis admin rotation -> one `issue_bundle` per
  included unit (after `api`, so bundles carry the rotated telemetry token).
- **Monitoring** is not delivered: the collector's PostgreSQL receiver
  (`cli/commands/observability/otel_collector.py`) dials the owner-only socket as
  `ava_monitor` by `peer`, so its rendered config names no credential and a
  rollout leaves it working.
- **Release** (`cli/release_transition/authority.py`, under the home operation
  lock, with `OperationAuthority(operation, direction)`): preflight
  `check_invariant` -> `fencing`: `revoke` -> owned pooler stopped (escalating;
  no listener left) -> `close_revoked` -> `prune` -> `authorizing`:
  `mint_generation` -> pooler serving the pending pair -> a pooled `SELECT 1`
  as each login -> `activate`; observation re-checks the invariant and that
  `stale_sessions` is empty. The journal records each step's intent and
  receipt ([[cli/release_transition/write-generations.ava.okf.md|release write generations]]).
