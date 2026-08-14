---
type: doc
title: ava cluster Subcommands
description: '`ava cluster ...` — verbs that act on a cluster rather than on this host''s services. Addressed by home path (`--path`), never by name: roster, down/destroy, rollback, health-probe, recover, and the OS-job registration verbs.'
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
| `restart` | bounce **the entire** cluster (local + fan-out, no git pull; POST `/api/cluster/restart`) |
| `down --path <home>` | stop the cluster at the home, keeping registry entry + data (safe stop for worktrees) |
| `destroy --path <home>` | stop + free the registry slot (port block); `--drop-db` deletes pg/redis data too; **refuses `~/.ava` (prod)** |
| `rollback` | commit-pinned (`apply_down` to target sha) |
| `health-probe` | health-probe cron payload (exit 0/1); **not** `cluster status` (roster GET). `--auto-rollback` **resets** the consecutive counter while a deploy is in flight (`ops/deploy_window.py`) — its checks fail by design mid-rollout, and the failures before a deploy were about the commit the deploy replaced |
| `recover` | clear a stranded update lock + pause; refuses while the holder pid lives |
| `health-probe-register` / `health-probe-unregister` | cluster-level cron |
| `watchdog-probe --role <cap>` | 60s OS job respawning that capability's dead watchdog — ends the who-watches-the-watchdog recursion (`-register` / `-unregister` variants) |

## Notes

- `down` and `destroy` differ in what survives: `down` keeps the registry slot
  and the data on disk (the safe way to stop a dev worktree cluster), `destroy`
  frees the port block. Only `destroy` can be told to drop the data.
- A destroyed home keeps its files, `.env` included — that `.env` is the only
  copy of the cluster's secret, of any key hand-added beyond `seed_allowlist()`, and
  of the URLs the data-plane identity is read from, so freeing a slot never
  discards credentials. (It would not strand the preserved pg data either way:
  `ensure_cluster_role` re-sets the role password to the current secret on every
  bring-up, so a rotation self-heals — the cost of losing `.env` is credentials
  and config, not data.) The leftover home stays *un-bootable* instead: the start gate
  (`cli/preflight.py:require_installed_home`) refuses a home the registry does
  not corroborate — no record, or a record whose port block the home's `.env`
  contradicts, which is what a since-reallocated block looks like from inside
  the stale home.
- `health-probe` is a cron payload that exits 0/1, not a human-readable view —
  the roster is `status`.
- `status`'s columns answer three different questions. `pin` and `code` are live
  per-host probe readings (checkout vs the cluster pin; the commit the answering
  process froze at). `hold` is not a probe at all: it is transcribed from the live
  `cluster_update_lock` lease — a banner above the table naming the lease, and
  `waited-on` on each host a settle hold's note records as still converging. So it
  explains a refused deploy without claiming to know convergence: `waited-on` is
  what the hold *says* it waits for, a blank cell is "not named by this hold" (a
  host that never acked never is), and a blank column is not proof no deploy runs —
  a watchdog-spawned host-local `ava-updater` takes no lease. The roster reads the
  lease row rather than `ops.deploy_window.deploy_in_flight()`, which probes every
  machine and releases a converged hold.

## Key dependencies

- [[cli.ava.okf.md]] — the CLI overview: why identity is path-only, and the top-level verbs
- [[cli/commands/commands.ava.okf.md]] — the module split these handlers live in
