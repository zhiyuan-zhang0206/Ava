---
type: doc
title: Machine API tokens
description: Per-generation machine API tokens that admit machine callers to the gateway API, bootstrap, webhooks and a unit's /ops, their launch delivery, and the telemetry relay token.
tags: [authority, auth, lifecycle]
---

# Machine API tokens

`api` is the HTTP half of the generation boundary: the database fence does not
stop a stale caller holding an HTTP bearer. Every generation secret carries one
32-byte token per class (`GenerationSecret.api`); the human cluster secret
stays on the gateway.

| Surface | Admits |
|---|---|
| gateway middleware, `/api/bootstrap`, alert/work-failed webhooks | the human secret, or the ACTIVE generation's gateway or runner token (`acceptance`, cached per ledger identity; `gateway.auth.request_principal.cluster_credential` records `cluster_bearer` or `machine_token:<class>`) |
| `/api/auth/login` | the human secret, or the active runner token (the managed browser's cookie); the session is bound to that credential and ends when it is revoked or rotated (`gateway.auth.request_principal.session_mints`) |
| a unit's `/ops` (`services/agent_ops/_boot._ops_acceptance`) | digests of its generation's gateway and runner tokens: a remote unit's capability carries the gateway digest, never the gateway token |

A revoked generation's token never authenticates again. The gateway re-reads
its acceptance on every request; a unit's `/ops` reads it once, at daemon
boot. That holds only because the ledger revokes in one place, the release
fence, and `LocalTransition.fence` first requires root and every service it
birthed, the ops daemon included, to be gone (`require_root_absent`). A new
revocation path must stop the ops daemon first, or `/ops` must read its
acceptance per request; `tests/lifecycle/db_authority/test_api_tokens.py`
fails on either change. Delivery mirrors the
login: the root launcher gives each service its class token (`api_access`,
`cli/commands/_data_plane.api_delivery`) only while the API is authenticated
(a set human secret on the gateway, an API-bearing capability on a remote
unit), so a delivered token always means "present a bearer"; the boot pass
gives an admitted operator process the same token; exec children and watchers
inherit it. Clients present `AVA_API_TOKEN` first, else the human secret
(`shared.cluster_auth.client_bearer`). An empty-secret single box delivers no
token and keeps its open API.

The OTLP relay ingress is telemetry, not a write path, and does not rotate per
generation: its bearer is `telemetry_token(secret)` (HMAC-SHA256 under the
human secret), so it changes only when that secret rotates; remote units
receive it in their capability.

The human-bearer rotation (`scripts/rotate_cluster_secret.py`, and once at the
fleet cutover, step `api` of `scripts/cutover_db_authority.py`) keeps the
logical-backup passphrase pinned (`services/gateway_side/backup/passphrase.py`:
minted at birth, `sha256(secret)` for a home the cutover converts), then
changes the secret; remote units then need new bundles for the new telemetry
token. Why: [rollout choices](../../../decisions/2026-09-27-write-generation-rollout-choices.md).
