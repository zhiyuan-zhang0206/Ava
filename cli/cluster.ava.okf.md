---
type: doc
title: ava cluster Subcommands
description: '`ava cluster ...` — verbs that act on a cluster rather than on this host''s services. Addressed by home path (`--path`), never by name: roster, down/destroy, prepared release operations, health-probe, recover, and the OS-job registration verbs.'
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
`cli/commands/cluster.py`, with registry allocation and `ls` / `down` /
`destroy` in `cli/commands/cluster_lifecycle.py`.

## Verbs

| Command | Function |
|---------|----------|
| `ls` / `status` | registered clusters / multi-machine roster |
| `update --prepared REQUEST` | submit or resume one captured release operation; no implicit source checkout update or target-machine shortcut. [[cli/release_transition/release_transition.ava.okf.md]] owns its input, native custody, and recovery contracts |
| `down --path <home>` | stop the cluster at the home, keeping registry entry + data (safe stop for worktrees) |
| `destroy --path <home>` | stop + free the registry slot (port block); `--drop-db` deletes pg/redis data too; **refuses `~/.ava` (prod)** |
| `health-probe` | Observation-only OS job (exit 0/1; wrong-checkout refusal is 2). Every outage episode persists its start in `$AVA_HOME/health_probe_alert`, stays silent through normal recovery, then grades WARNING → ERROR. A live deploy lease pauses explained grading without resetting its start; disk pressure remains independent. Low agent population remains unhealthy during local maintenance and keeps global alert grading. The probe neither rolls back releases nor publishes known-good state. Provider balance and halted-agent checks remain part of health observation. |
| `recover` | clear a stranded update lock + pause; refuses while the holder pid lives |
| `health-probe-register` / `health-probe-unregister` | Register/remove the observation-only OS job; registration accepts its interval, with no rollback threshold |
| `pitr status\|activate\|rollback` | durable physical-backup activation lifecycle; the first delivery validates shadow readiness and creates the mandatory logical recovery floor, then stops before PostgreSQL mutation |

## Notes

- `down` and `destroy` differ in what survives: `down` keeps the registry slot
  and the data on disk (the safe way to stop a dev worktree cluster), `destroy`
  frees the port block. Only `destroy` can be told to drop the data.
- A destroyed home keeps its files, `.env` included — that `.env` is the only
  copy of the cluster's secret, of any explicitly configured provider credentials, and
  of the URLs the data-plane identity is read from, so freeing a slot never
  discards credentials. (It would not strand the preserved pg data either way:
  `ensure_cluster_role` re-sets the role password to the current secret on every
  bring-up, so a rotation self-heals — the cost of losing `.env` is credentials
  and config, not data.) The leftover home stays *un-bootable* instead: the start gate
  (`cli/start_identity.py:_validate_existing`) refuses a home the registry does
  not corroborate — no record, or a record whose port block the home's `.env`
  contradicts, which is what a since-reallocated block looks like from inside
  the stale home.
- `health-probe` is a cron payload that exits 0/1, not a human-readable view —
  the roster is `status`.
- `status`'s `code` column is a live per-host probe reading: the commit the
  answering process froze at, marked when it differs from the host's checkout
  HEAD. There is no cluster pin or known-good column: nothing writes those legacy
  values, so a verdict against them would present a frozen value as current. The
  deploy-hold banner above the table is not a probe: it is transcribed from the
  live `deployment_state` lease and explains a refused lease acquire. Its absence
  is not proof no deploy runs — native admission and maintenance holds are
  separate facts. The roster reads the lease row rather than
  `ops.deploy_window.deploy_in_flight()`, which probes every machine and releases
  a converged hold.

## Key dependencies

- [[cli.ava.okf.md]] — the CLI overview: why identity is path-only, and the top-level verbs
- [[cli/commands/commands.ava.okf.md]] — the module split these handlers live in
