---
type: doc
title: ava cluster Subcommands
description: '`ava cluster ...` — verbs that act on a cluster rather than on this host''s services. Addressed by home path (`--path`), never by name: roster, down/destroy, health-probe, and the OS-job registration verbs.'
tags:
- cli
- cluster-lifecycle
---

# `ava cluster` Subcommands

## What it is

The verb group that acts on **a cluster** rather than on this host's local
services. `ava start` / `ava stop` / `ava status` operate on whatever cluster
this checkout belongs to; `ava cluster ...` can name a different one.

A cluster's identity **is** its home path, so every verb that names one takes
`--path <home>` — there is no cluster name to pass. Handlers live in
`cli/commands/cluster/control.py`, with `down` / `destroy` in
`cli/commands/cluster/home.py`. The host keeps no list of clusters: each home
describes only itself (`base/cluster/record.py`).

## Verbs

| Command | Function |
|---------|----------|
| `status` | this cluster's multi-machine roster |
| `db-authority issue-unit` | gateway only: seal one remote unit's capability bundle (its join, emergencies) ([[base/cluster/authority/docs/wiring.ava.okf.md]]) |
| `down --path <home>` | stop the cluster at the home, keeping its record + data (safe stop for worktrees) |
| `destroy --path <home>` | stop, retire its OS jobs and checkout binding, mark the home detached; `--drop-db` deletes pg/redis data too; **refuses `~/.ava` (prod)** |
| `health-probe` | Observation-only OS job (exit 0/1; wrong-checkout refusal is 2). Every outage episode persists its start in `$AVA_HOME/health_probe_alert`, stays silent through normal recovery, then grades WARNING → ERROR. An open deploy window (a cohort machine whose `host_deploy_state` posture is not `idle`, read through `ops.deploy_window.deploy_in_flight()`) pauses explained grading without resetting its start, and disk pressure remains independent. Low agent population remains unhealthy during local maintenance and keeps global alert grading. The probe neither rolls back releases nor publishes known-good state. Provider balance and halted-agent checks remain part of health observation. |
| `health-probe-register` / `health-probe-unregister` | Register/remove the observation-only OS job; registration accepts its interval, with no rollback threshold |

## Notes

- `down` and `destroy` differ in what survives: `down` leaves a startable home
  (the safe way to stop a dev worktree cluster), `destroy` marks it detached so it
  never starts again. There is no slot to free: a stopped home's ports are simply
  unbound. Only `destroy` can be told to drop the data.
- A destroyed home keeps its files, `.env` included — that `.env` is the only
  copy of the cluster's secret, of any explicitly configured provider credentials, and
  of the URLs the data-plane identity is read from, so detaching never
  discards credentials. (It would not strand the preserved pg data either way:
  `ensure_cluster_role` re-sets the role password to the current secret on every
  bring-up, so a rotation self-heals — the cost of losing `.env` is credentials
  and config, not data.) The leftover home stays *un-bootable* instead: its
  `destroy-intent.json` is `detached`, which `cli/start_identity.py:prepare_identity`
  refuses.
- `health-probe` is a cron payload that exits 0/1, not a human-readable view —
  the roster is `status`.
- `status`'s `code` column is a live per-host probe reading: the commit the
  answering process froze at, marked when it differs from the host's checkout
  HEAD. There is no cluster pin or known-good column: nothing writes those legacy
  values, so a verdict against them would present a frozen value as current. The
  roster carries no deploy-window verdict: `ops.deploy_window.deploy_in_flight()`
  reads every machine's posture row, which does not belong on a read-only roster
  GET.
- There is no `cluster` verb for a stranded maintenance hold: read it with
  `ava maintenance status` and end it with `ava maintenance resume --cancel` or
  `repair` ([[conventions/graceful-maintenance.md]]).

## Key dependencies

- [[cli.ava.okf.md]] — the CLI overview: why identity is path-only, and the top-level verbs
- [[cli/commands/docs/commands.ava.okf.md]] — the module split these handlers live in
