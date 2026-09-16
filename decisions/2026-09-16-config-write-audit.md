# Config write audit v2: actor + value diff in the .env JSONL (task #3588)

## Context

Config writes all went through audited paths, but the record answered only "which keys landed, from
which site/process". The `AVA_HOST_MAX_CONCURRENT_TURNS=50` change of 2026-09-11 (audit record #18)
could not be attributed or reconstructed afterwards: pid and site name the machinery, not the
initiator, and nothing carried the old→new. Task #3588 requires every write to leave a queryable
record (who / when / key / old→new) or an explicit won't-do.

Constraints that shaped the sink:
- Write paths span contexts without DB identity (install/converge, CLI on a fresh box, secret
  rotation scripts) — the sink must work wherever a home exists.
- `.env` is the only on-disk copy of cluster secrets; the record must never widen where secrets can
  appear (support bundles, backups, the shared event stream, cross-machine reads).
- Request identity already exists server-side (`source_verified_by` + principal, bound by the auth
  middleware) — attribution must come from there, never from caller JSON.

## Decision

Extend the existing per-home JSONL (`$AVA_HOME/.env.audit.jsonl`, 0600) additively — record v2:

- `actor: str | null` — the initiating credential fact: `user_session:<subject>`,
  `cluster_bearer:<subject>`, or `cli:<os-user>`; null where no identity exists (converge, scripts).
- `trace_id: str | null` — the gateway request trace when one carried the write.
- `changed: [{alias, scope, sensitive, old, new}]` — the old→new diff, captured under the env lock
  before the rewrite. Values survive **only for aliases registered with `sensitive: false`**; a
  sensitive or unregistered alias keeps its name with `old`/`new` null; a metadata lookup failure
  withholds every value (fail closed). Unset is `new: null`.
- The `env_write` event gains `actor` and stays value-free.

Identity is server-stamped on the existing hop: `PUT /api/config` reads request state, passes
`actor`/`trace_id` to `write_fields` and into the `config_write` op payload (optional fields, so the
daemon stamps nothing itself); the CLI local write records `cli:<os-user>`. This PR is record +
identity; the query surface (CLI `ava config audit`, `GET /api/config/audit`, `config_audit_read`
op) is the separate second PR. Additive-only: no config value or write semantics change.

## Alternatives rejected

- **Cluster DB `config_audit` table** — the write paths run in contexts with no DB identity while
  the JSONL sink works wherever a home exists; a cluster-shared table widens the blast radius of
  non-secret values for no new capability, adds migration/retention/RLS surface, and duplicates the
  sink into two truths. The event stream already gives the cluster roll-up; v2 extends it with the
  actor only.
- **Hash values for sensitive keys** — low-entropy secrets allow dictionary verification; "no
  value-derived data for sensitive" is cleaner and keeps the existing promise.
- **Caller-supplied actor** — attribution read from request JSON would be forgeable; only
  middleware-verified state is used.
- **Agent-level attribution behind the shared cluster bearer** — deferred: a bearer caller is not
  distinguishable without a new identity mechanism; trace_id + gateway logs are the correlation
  bridge for now.

## Consequences

- Old records stay readable (fields are additive; `actor`/`trace_id`/`changed` are optional).
- The JSONL now holds non-secret configuration content: backups and support bundles inherit that
  handling (the file is already owner-only 0600); sensitive changes stay value-blind by design.
- `changed` makes no-op rewrites visible (`old == new`) — the WSL converge rewrite noise becomes
  measurable instead of invisible; its fix is tracked separately.
