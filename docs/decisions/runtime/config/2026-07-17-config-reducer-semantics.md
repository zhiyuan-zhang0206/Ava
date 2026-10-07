# PUT /api/config is a reducer (JSON merge patch), not a full-replace

## Context

`PUT /api/config` was full-replace: the body was the whole desired override set,
and any managed field set in `.env` but ABSENT from the body was unset. An agent
sent a one-field write (`{anthropic_api_key: ...}`) through it; every other
cluster-pinned key — all provider API keys, the telegram token, `AVA_CLUSTER_SECRET`
— was absent, so all of them were silently unset. Running processes held the old
values in memory and stayed up, so it was invisible for ~2h; the next `ava update`
restarted every service, and agents (which cold-start from `.env` alone) crashed —
redis auth failed (no secret → identity not rewritten → connected as `default`) and
the model factory found no API key. The `.env` was the only on-disk copy of those
secrets; the running process env was the sole surviving copy.

The frontend never needed full-replace: its delta helper only ever *sets* keys, and
there is no UI to clear one. `ava config unset` needed deletion, but implemented it
by omitting the key from an otherwise-full body — the same delete-by-absence that
did the damage.

## Decision

`PUT /api/config` is a **JSON merge patch (RFC 7386) / reducer**: a key with a value
is set/replaced, a key mapped to `null` is unset (reverted to default), and an
ABSENT key is left untouched. Deletion is always the explicit `null`, never inferred
from absence. Both scope paths (cluster in the gateway's `.env`, host via
`config_write_op`) and the `ava config` CLI (`unset` now sends `{key: null}`, `set`
sends only the changed key) follow it. Independently, every `.env` write
(`write_fields`, `upsert_env`) snapshots the file to `<home>/backups/env/` first and
logs any unset — the write is recoverable and never silent.

## Alternatives rejected

- **Keep full-replace, add `?replace=true` opt-in.** Smallest diff, but the footgun
  survives as the default the incident already hit, and it leaves the real latent
  bug — the frontend builds its "full" body from a react-query *cache snapshot*, so
  a stale cache (another process added a key) means an unrelated toggle drops that
  key by absence. Merge semantics dissolve that TOCTOU race for free: absent = leave
  alone.
- **Explicit `removals: [...]` list in the body.** Also kills delete-by-absence, but
  it's a heavier wire-contract change (body shape, frontend, generated types) for a
  capability `null` already expresses. No caller needs to name a set of deletions
  atomically.

## Consequences

- Deletion requires an explicit `null` — a client that wants to clear a field must
  say so. `ava config unset` and any future "reset to default" UI send `{key:
  null}`; there is no other delete path, by design.
- Clients send only what they change; echoing the whole override set back is
  unnecessary and (with a stale snapshot) unsafe.
- `.env` writes carry a rolling backup + an audit log line. The backup is
  best-effort (a snapshot failure is logged, never blocks the write it protects).
