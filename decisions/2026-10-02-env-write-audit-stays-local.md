# The `.env` write audit stays in the per-home JSONL

## Context

`decisions/2026-10-02-audit-events-in-postgres.md` makes `audit_events` the record of every
`category=audit` event and requires each emit site to record there first. The `env_write` and
`env_unauthorized_write` events are registered as audit events, but their record was decided
earlier (`2026-09-16-config-write-audit.md`): a per-home owner-only JSONL
(`$AVA_HOME/.env.audit.jsonl`), because the writers run in contexts with no database identity
(install and converge, the CLI on a fresh box, secret-rotation scripts) and a cluster table
would widen where configuration content appears.

## Decision

The `.env` write audit keeps the per-home JSONL as its record. The `env_write` /
`env_unauthorized_write` events are a value-free projection to the unified stream and are not
recorded in `audit_events`. The audit-record lint lists the one emit site with this reason
instead of baselining it.

## Alternatives rejected

- **Also write to Postgres when it is reachable.** A second record whose presence depends on
  reachability is two truths that disagree exactly when something is wrong; the JSONL already
  exists everywhere a home does.
- **Reclassify the two events as telemetry.** That moves them out of the audit view and the
  business tier for no gain in durability; the history query is `ava config audit` over the JSONL.

## Consequences

- The Loki projection of these two events lasts 84 hours; the JSONL is the history.
- This is the only audit event whose record is not `audit_events`; a new exception needs its own
  entry in the lint and a decision.
