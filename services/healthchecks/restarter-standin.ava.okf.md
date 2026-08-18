---
type: doc
title: The restarter's stand-in dispatch
description: The one healthcheck that does work beyond probe+respawn and the only one that touches the DB — what it dispatches, when it runs, why it delegates to the daemon's own RespawnController rather than re-implementing the dispatch, and the two bounds it works under.
tags:
- ops
---

# The restarter's stand-in dispatch

## What it is

`services/healthchecks/restarter.py:_standin_dispatch` dispatches this host's `restarting` rows once, standing in for a daemon that could not be revived. Every other daemon healthcheck is probe + respawn and nothing else; this is the exception, and the only healthcheck that opens a DB connection.

## When it runs

Only when the round will have **no live daemon**: the respawn failed to verify, or the verdict was terminal so no respawn was attempted at all ([[services/healthchecks/terminal-verdict/terminal-verdict.ava.okf.md]] — another cluster holds the port, so no daemon of ours can ever bind it). The restarter daemon is what dispatches restarts, so while it stays down every `restarting` row on this host is frozen — the 98-minute shape of the 2026-07-24 outage, with the DB and gateway perfectly healthy throughout. On the terminal path "until a human intervenes" is literal, which is exactly where this has to carry the round.

Not on the success path: a revived daemon's own `RespawnController` sweeps the same rows on its first tick (~1s), with a machine scope and gateway-health gate.

## Why it delegates

It calls `ops.controllers.respawn.RespawnController` rather than re-implementing the dispatch. The hand-rolled copy it replaces was neither machine-scoped nor gateway-health-gated: it respawned OTHER machines' agents on this host, which trips the boot placement gate (`agent/_starting.py`) and **burns the restart** — the CAS has already moved the row off `restarting`, so the rejected boot leaves a corpse and the request itself is gone. Recovery then rests on CrashResurrect, which only claims rows holding a pending inbound in its workload allowlist; a restart is not in it.

## Its bounds

The general rule it obeys — nothing failable ahead of the respawn — is [[probe-contract/main-ordering.ava.okf.md]]; this is the healthcheck that broke it and the reason it is written down. The invariant is now structural rather than per-module: `shared.service_respawn.run_keepalive` calls its `on_unrevivable` hook only after a respawn attempt, or in place of one that would be futile.

Two bounds follow from running inside the watchdog's sequential tick: it is **total** (never raises, so it cannot mask the non-zero exit that reports the respawn failed) and it passes a short `shared.db.pool(timeout=…)` rather than psycopg_pool's 30s default, because a half-dead DB here delays every check queued behind it.

## Key Dependencies
- [[services/healthchecks/healthchecks.ava.okf.md]] — the per-service probe/restart roster this is the exception to
- [[probe-contract/main-ordering.ava.okf.md]] — the ordering rule this healthcheck's history produced
- [[services/healthchecks/terminal-verdict/terminal-verdict.ava.okf.md]] — the verdict that makes the stand-in indefinite
