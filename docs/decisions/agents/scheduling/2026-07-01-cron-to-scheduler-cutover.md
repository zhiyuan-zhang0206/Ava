# Cron → scheduler cutover: gate the destructive drop behind the runbook, not the merge

> **Decision date 2026-07-01** (`037cb9e1`). **Complete** — the gateway-hosted
> **schedules** subsystem (`gateway/schedule_manager.py`, `/api/schedules`, the
> `ava_schedule_writer` skill, `/control/schedules`, `ava schedules …`) is the only
> scheduler; `gateway/cron_scheduler.py`, `/api/cron/jobs`, the Cron settings tab,
> and the `cron_jobs` table are all gone.

## Decision

Replace `cron_jobs` with `schedules`, and **do not let the table drop ride the
merge**. `ava update` applies pending migrations automatically, so shipping the
removal and the drop together would drop `cron_jobs` the moment the code deployed —
and the same PR deletes the converter (`gateway/schedule_convert.py`) that reads it.
A live, unconverted cron job would then silently stop firing with no way to recover
its definition.

So the work split across two deploys with an operator step in between: PR1–5 ran
both systems side by side and shipped the converter; the operator converted, cut
over, and verified job by job; only then did PR6 land the removal + the drop
(migration `0067_drop_cron_jobs`).

## Why this shape

- **Expand-contract, applied to a data-carrying table.** The generic rule is that a
  lossy operation gets its own migration, decoupled from the commit that stops using
  the data. Here the decoupling has to be *wider* than one commit, because the data
  is a live behavior (a firing job), not just rows — the safe unit is "verified in
  production", not "no longer referenced in code".
- **Reversibility was not enough.** `0067` ships a `.down.sql` that recreates the
  table, but **empty** — the down migration restores the schema, not the jobs. A
  reversible migration is not a substitute for converting first.
- **The converter is deliberately conservative.** It creates every schedule
  `enabled=false` and is idempotent (skips names already present), so running it
  early or twice is safe and nothing double-fires. Each generated script reuses the
  agent by label (resurrect/wake) rather than always spawning — the reuse the old
  cron path lacked, and the reason converted jobs are strictly better than a
  mechanical translation.
- **One job at a time, never both live.** Enable the schedule, disable the cron row,
  verify, next. The cron rows carry no live state once disabled, which is what makes
  the final drop boring.

## The procedure that was run (record)

1. **Deploy PR1–5.** Schedules run alongside cron; nothing dropped; the converter is
   present.
2. **Convert** — `convert_cron_jobs_to_schedules(conn)` from
   `gateway/schedule_convert.py`, creating a disabled schedule per cron job.
3. **Cut over each job**: enable its schedule (`POST /api/schedules/{id}/start` or
   the `/control/schedules` toggle), disable the cron job (`PUT /api/cron/jobs/{id}`
   `{"enabled": false}`), verify it fires and reuses/spawns the right agent.
4. **Confirm** every cron row disabled and every equivalent schedule running.
5. **Deploy PR6** — removes `gateway/cron_scheduler.py`, `gateway/routers/cron.py`,
   `gateway/schedule_convert.py` (spent), the `/api/cron/jobs` routes, the Cron
   settings tab, and the CronJob API client + types; applies `0067` and syncs
   `db/schema.sql`. `skills/ava_guide` repoints at schedules /
   `ava.skills.ava_schedule_writer` instead of the removed `reference/cron.md`.

Rollback posture at the time: before step 5 there was nothing to roll back (cron
intact, just disabled per job); after step 5, a misbehaving schedule is fixed
forward by editing its script on `/control/schedules`, since rolling the schema past
`0067` would only restore an empty table.
