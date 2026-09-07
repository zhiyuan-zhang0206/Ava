---
type: doc
title: Block Scope — how much of a round a reconcile gate holds back
description: A blocking watchdog controller reports a BlockScope (blast radius), each service declares ServiceSpec.requires_db (what it needs), and the watchdog matches the two — so a Postgres outage no longer strands the services that never touch Postgres.
tags: []
---

# Block Scope — how much of a round a reconcile gate holds back

## What it is
`BlockScope` (`ops/controllers/base.py`) is what a blocking controller returns on `ReconcileResult.blocks`. Three nesting values: `NONE` ⊂ `DB_DEPENDENT` ⊂ `ALL`.

- **`NONE`** — nothing held back; the round runs its full roster.
- **`DB_DEPENDENT`** — the cluster's Postgres is unusable *from here* (unreachable, or its applied migrations disagree with this checkout). Only services that read or write it are held back: reviving one spawns a daemon that dies in its own `assert_schema_current` and crash-loops once a round.
- **`ALL`** — this host is mid-transition of its own code or processes (paused by a rollout, an `ava update` spawned, an off-pin force-update). Everything is about to be restarted by that transition, so reviving anything fights it.

## The division of knowledge
A controller states only the **blast radius of its own finding** and never names services. Each service states what it needs, on its own spec: `ServiceSpec.requires_db` (required, no default — see [[services/services.ava.okf.md]]). `services/watchdog/daemon.py:_checks_for_round` is the ONE place the two meet: `ALL` runs nothing (returning before the roster is even built, so a paused host costs nothing per round), `DB_DEPENDENT` keeps the `requires_db=False` entries, `NONE` keeps everything. It is total over the enum — an unhandled member raises rather than defaulting to "run everything" (unsafe) or "run nothing" (the bug below).

The hand-added pseudo-checks classify themselves the same way: `redis-acl`, `pgbouncer`, `lgtm`, `brew-pin`, and `permissions-helper` are DB-free (`False`); `station-probe` is DB-dependent because it resolves the remote station from Postgres. `pg-backup` is a regular DB-dependent `ServiceSpec` service.

## Why (the defect it replaced)
`blocks_tick: bool` conflated "my own reconcile failed" with "nothing else should run either", which forced one controller's local failure to be a global verdict. The schema controller's DB-unreachable arm skipped the WHOLE agent-runner roster, so `browser` / `browser-mcp` — no DB at boot, none at runtime ([[services/agent_runner_side/browser/browser/browser.ava.okf.md]]) — went unrevived for the entire duration of a database outage. A DB outage took out the recovery path for an unrelated Chrome crash, and it did so through a controller deciding on behalf of services it knows nothing about.

## Scope per gate
| Gate | Scope | Why |
|---|---|---|
| updater — hung `ava-updater` reaped | `NONE` (always) | the only gate that never blocks: it deletes a stale signal rather than moving a service, and the gates behind it were deferring to that signal, so they must run this round on the corrected reading |
| pause | `ALL` | a rollout deliberately took every service here down |
| schema — updater spawned (or one already in flight) | `ALL` | that update replaces every process on this host |
| schema — code-behind-DB in cooldown, in heal backoff, or both trigger paths failed | `DB_DEPENDENT` | nothing is transitioning the host; the finding is just "this code is behind the migrated DB" |
| schema — `SchemaVersionMismatch` (awaiting gateway migration) | `DB_DEPENDENT` | the wait lasts as long as the gateway takes; holding a browser down buys nothing |
| schema — DB unreachable / any other exception | `DB_DEPENDENT` | a DB outage is exactly when an unrelated crash's recovery path must keep working |
| pin — force-update spawned | `ALL` | it rewrites the checkout and restarts everything |
| code — stale-process restart spawned | `ALL` | same, minus the checkout |

## A blocked round is never silent
`ops/manager.py` logs every blocked round: which dimension blocked, the scope, the controller's `detail`, and how many **consecutive** rounds the streak has run — WARNING, escalating to ERROR at `_BLOCKED_ROUND_ALARM_ROUNDS` (10 rounds ≈ 10 min — "no legitimate rollout is longer than this"; `STRANDED_PAUSE_TIMEOUT_S` no longer shares that judgment, since it now bounds only a provably unowned pause). The round that clears a streak logs too, so the gap has an end timestamp and the counter resets.

Why it is logged every round and not once on change: a skipped round and an all-green round were previously **indistinguishable in the log**, so a Windows runner's 3h07m window with zero roster reconciles (2026-07-28 22:04 → 2026-07-29 01:11, blocked every minute by `Schema ahead of code` while its `ava update` trigger kept failing ~85 times) could only be found forensically, by counting `__main__:_run_check` lines per hour. Absence of output was the only evidence.

The per-round line is an alarm, never a rate limit. Bounding the *action* a block retries — the ~85 failed `ava update` triggers of that window — is a separate mechanism, and a backed-off round still reports its block: [[services/watchdog/block-scope/heal-backoff.ava.okf.md]].

## DB-free services today
`browser`, `browser-mcp`, `milvus`, `memory-indexer`, `frontend`, plus the `redis-acl`, `pgbouncer`, `lgtm`, and `brew-pin` pseudo-checks. Everything else on the roster calls `assert_schema_current` at boot and then reads or writes the DB.

## Key dependencies
- [[services/watchdog/watchdog.ava.okf.md]] — the tick that consumes the scope
- [[services/services.ava.okf.md]] — `ServiceSpec`, where `requires_db` is declared

## Entry points
- `ops/controllers/base.py` — `BlockScope` + `ReconcileResult.blocks`
- `ops/manager.py` — short-circuits on the first controller that blocks anything, passes its scope up
- `services/watchdog/daemon.py` — `_checks_for_round`, the scope↔roster match
