# `.env` backup filenames use the host clock, not the cluster clock

## Context

User ruling 2026-08-27 (Task #1758,
[`decisions/2026-08-27-cluster-timezone-wall-clock.md`](../../runtime/hosts/2026-08-27-cluster-timezone-wall-clock.md))
made the cluster's `AVA_TIMEZONE` the one wall clock every display path
renders through, and named `shared.envfile` backup-name stamps as one of
those paths.

`shared.envfile.snapshot_env` backs up a unit's `.env` before every rewrite
(`upsert_env`, `remove_env`, `replace_env_bytes_cas`) into
`$AVA_HOME/backups/env/.env.<stamp>`, and it is deliberately called from
those doors while holding the `.env` lock, before the write it protects.
Reading the cluster clock means resolving `cluster_tz()`, which needs a built
`Settings` instance (`shared/config/_lite.py:prepare`, `field_explicitly_set`)
— `load_ava_env()` plus data-plane source resolution, and on a pure
agent-runner without a local `.env` yet, a `GET /api/bootstrap` call. One of
`snapshot_env`'s callers writes `.env` before that is safe to do: a unit's
first `ava start`, which publishes the unit's identity keys into `.env`
(`cli/start_identity.py`) before machine identity is resolved — on a gateway
home and on a remote agent-runner joining with `--gateway-url` and
`--db-capability` alike. At that point, loading runtime config has side
effects this writer cannot afford, and can fail outright — turning a
best-effort backup into an occasional first-start crash.

## Decision

`snapshot_env`'s backup-filename stamp is `datetime.now().astimezone()` —
the process's own local wall clock, whatever the host OS zone is set to —
never `cluster_tz()` and never `settings.general.timezone`. This is a named
exception to the 2026-08-27 one-cluster-clock rule, scoped to exactly this
one call site: every other display path that decision named
(`shared.alerts.format_local`, `ops.cluster_status`, `shared.memory_repo`,
`ava.agents`, the watcher announcement) keeps going through `cluster_tz()`
unchanged.

The backup filename's stamp exists only to order and de-duplicate snapshots
on disk (`snapshot_env`'s dedupe-on-identical-content check, and the
newest-`N`-kept prune) — nothing parses it back into a cluster-relative
instant, and nothing compares it against a cluster-clock-rendered timestamp
from another surface. The only reader is a human scanning
`backups/env/` by eye, for whom the host's own clock is the least surprising
answer at the one moment reading the cluster clock is unsafe.

## Alternatives rejected

- **Keep `cluster_tz()`, accept the config-load cost.** Rejected: the whole
  reason this writer exists is to run safely at points before identity is
  established, including on a pure runner. Loading Settings there reintroduces
  exactly the failure mode `snapshot_env` must not have — a backup attempt
  that can itself crash the write it was meant to protect. (A prior form of
  this same problem already surfaced once: `tests/cli/test_config_cmd.py`'s
  `test_local_set_repairs_incident_env` is a regression test for a version
  where the backup snapshot forced Settings construction and crashed
  `config set --local`'s repair path on a broken `.env`.)
- **Force explicit UTC.** Rejected: it still diverges from "one cluster
  clock" (now two clocks instead of one: cluster time everywhere else, UTC
  here), and it is the least intuitive choice for the one reader this stamp
  serves — an operator's own machine rarely displays itself in UTC, so a UTC
  stamp is a translation step for every reading, not fewer of them.

## Consequences

- A runner whose host OS zone differs from the cluster's sees `.env` backup
  names stamped in its own zone, while every other timestamp on that same
  runner (logs, watcher output, alerts, status snapshots) renders in the
  cluster zone. This is a legibility-only divergence: the stamp is never fed
  back into cluster-relative logic, so it affects only how the filename reads
  to a human, not correctness.
- `snapshot_env` stays callable before Settings can be built at all — no
  config load, no network call, no way for a `.env` backup to fail because
  the cluster clock could not be resolved.
