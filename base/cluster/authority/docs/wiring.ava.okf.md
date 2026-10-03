---
type: doc
title: Write-generation delivery and wiring
description: How the active write generation reaches the pooler userlist, launched services, admitted operator processes and remote agent-runner units (login and API token), and the birth and ordinary-start call sequences.
tags: [postgres, authority, lifecycle]
---

# Write-generation delivery and wiring

## Delivery

`delivery` hands the active generation to its three consumers, each bound to
the ledger's credential digest:

- `render_userlist`: the pooler's `auth_file` — the generation's two SCRAM
  verifiers plus the admin-console entry `ava_pooler_admin`
  (`pooler-admin.json`, created once by birth, never a PostgreSQL
  role). Deterministic bytes, so an unchanged userlist is never restarted.
- `write_grant`: one class login and API token of the ACTIVE generation (a
  pending one is never delivered) for a launch environment;
  `reference` is the only part a launch digest or journal carries.
- `consume`: the gateway login (and, while the API is authenticated, the
  gateway API token) for an operator process the launcher did not inject, only
  after `require_admitted_runtime` (the start intent's source checkout); `operator_environment` is that delivery as
  environment, refused for a launcher-started process.

## Machine API tokens

The generation's API tokens gate the HTTP API and `/ops`:
[[base/cluster/authority/docs/api-tokens.ava.okf.md|Machine API tokens]].

## Remote agent-runner units

Bootstrap (`GET /api/bootstrap`) serves the credential-free endpoint and no
login, and a runner strips any password an older gateway still serves
(`base.host.env.bootstrap.fetch_bootstrap_config`). `unit` is the only path that
carries a login to another home:

- **Issue** (gateway, `ava cluster db-authority issue-unit --machine M --home H
  --out FILE`): `issue_bundle` seals the ACTIVE generation's runner login, the
  unit's API admission (`UnitApi`: the runner API token, the gateway token's
  digest and the telemetry token; none when the gateway's API is open), the
  endpoint bootstrap serves (`service_read.served_db_endpoint`) and a binding
  (machine, home, generation number and credential digest, nonce, expiry) with
  AES-256-GCM under a fresh 32-byte transport key.
  The key is printed once and stored nowhere; the header (machine, home,
  generation, expiry, nonce) is readable and authenticated as associated data.
  Refused on a pure runner, a remote-managed plane (no generation exists there),
  a home without an active generation.
  Nothing in the bundle is the unit's own; the login and tokens are shared
  ([what a bundle exposes](unit-bundle.ava.okf.md)).
- **Install** (unit, `ava init --db-capability FILE` for the first join and
  `ava cluster db-authority install-unit FILE` for a later bundle, key in
  `AVA_DB_CAPABILITY_KEY`, popped at once): `open_bundle` refuses anything that
  fails authentication or has expired; the join's bootstrap fetch presents the
  bundle's API token (never the human secret); `install_bundle` requires this machine
  name and home, the endpoint the gateway serves now, the installed generation's
  credential digest, and a login the cluster accepts (`SELECT 1` through the
  endpoint). It writes `unit.json` (0600) and start
  deletes the bundle. A first join with no
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

## Unit bundle exposure

What a bundle carries and who shares it:
[unit bundle exposure](unit-bundle.ava.okf.md).

## Wiring

- **Birth** (`cli/commands/_data_plane.complete_gateway_data_plane`, start
  intent `configured`): `ensure_groups` -> `ensure_monitor` ->
  `retire_legacy_logins` ->
  `create_ledger` -> `ensure_pooler_admin` -> `mint_generation` -> pooler
  serving the pending pair -> a pooled `SELECT 1` as each login -> `activate`.
  A retry reconciles to the same generation 0.
- **Ordinary start**: `ensure_groups` after migrations -> `ensure_monitor` ->
  `check_invariant` (read-only grantees such as `grafana_ro`); a home
  with no ledger is refused before any native effect.
- **Launch**: the root launcher delivers `AVA_DB_URL` + `AVA_DB_GENERATION` per
  service class; `base/dotenv_boot` keeps a delivery naming this home's
  endpoint and consumes the gateway login for an admitted operator process;
  otherwise the first dial raises `NoDatabaseAuthorityError`.
- **Monitoring** is not delivered: the collector's PostgreSQL receiver
  (`cli/commands/observability/otel_collector.py`) dials the owner-only socket as
  `ava_monitor` by `peer`, so its rendered config names no credential.
