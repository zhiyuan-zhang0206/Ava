---
type: doc
title: Write-generation delivery and wiring
description: How the active write generation reaches the pooler userlist, launched services and admitted operator processes, and the birth, ordinary-start and cutover call sequences.
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
  invariant -> credential-free `.env`.
- **Monitoring** is not delivered: the collector's PostgreSQL receiver
  (`cli/commands/_otel_collector.py`) dials the owner-only socket as
  `ava_monitor` by `peer`, so its rendered config names no credential and a
  rollout leaves it working.
- The release transition's fence/authorize phases are not wired yet.
