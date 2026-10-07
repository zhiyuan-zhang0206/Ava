---
type: doc
title: ava cluster Subcommands
description: '`ava cluster ...` — verbs that act on the cluster rather than on this host''s services: roster, destroy, health-probe, and the OS-job registration verbs. The home is `AVA_HOME` (else `~/.ava`), never a path argument.'
tags:
- cli
- cluster-lifecycle
---

# `ava cluster` Subcommands

## What it is

The verb group that acts on **the cluster** rather than on this host's local
services. The host runs one cluster, in the home `AVA_HOME` names (else `~/.ava`),
so no verb takes a path or a name. Handlers live in
`cli/commands/cluster/control.py`, with `destroy` in
`cli/commands/cluster/home.py`. The host keeps no list of clusters: each home
describes only itself (`base/cluster/record.py`).

## Verbs

| Command | Function |
|---------|----------|
| `status` | this cluster's multi-machine roster |
| `db-authority issue-unit` | gateway only: seal one remote unit's capability bundle (its join, emergencies) ([[base/cluster/authority/docs/wiring.ava.okf.md]]) |
| `db-authority install-unit BUNDLE` | an initialized agent-runner unit only: install a bundle sealed for it, the same join `ava init --db-capability` makes (a fresh expiry or a rotated telemetry token; stop the unit first); the key comes from `AVA_DB_CAPABILITY_KEY` and the bundle file is deleted once installed. Refused on a gateway home and on an uninitialized home (`cli/unit_join.py`) |
| `destroy` | decommission this host's cluster: stop it, retire its OS jobs and the permissions helper, mark the home detached; `--drop-db` deletes pg/redis data too. Asks for the home path typed at a terminal: stdin and stdout must both be terminals, and no flag skips the prompt |
| `health-probe` | Observation-only OS job (exit 0/1; wrong-checkout refusal is 2). Each unhealthy run emits one `health_probe_failing` event (`check`, `failure_class` of `code` / `environment` / `maintenance` / `alert-only`, `message`) and a healthy run emits nothing; the event repeats on every run while the condition holds, and Grafana alert rules read it (`for:` grades WARNING then ERROR; a deploy window is a Grafana silence). The probe keeps no state and notifies no one. The probe neither rolls back releases nor publishes known-good state. Provider balance and halted-agent checks remain part of health observation. |
| `health-probe-register` / `health-probe-unregister` | Register/remove the observation-only OS job at its default interval; no rollback threshold |

## Notes

- Stopping without decommissioning is `ava stop`; `destroy` additionally marks the
  home detached so it never starts again (deleting `destroy-intent.json` by hand is
  the only way back). There is no slot to free: a stopped home's ports are simply
  unbound. Only `destroy` can be told to drop the data, and its prompt then lists
  the directories it will delete. It acts on the default home too: the typed path
  is the guard.
- A home that is not the default home neither registers nor removes OS jobs
  (`base.host.system.cron.owns_os_jobs`): their labels and crontab markers name the
  job, not a home.
- A destroyed home keeps its files, `.env` included — that `.env` is the only
  copy of the cluster's secret, of any explicitly configured provider credentials, and
  of the URLs the data-plane identity is read from, so detaching never
  discards credentials. (It would not strand the preserved pg data either way:
  `ensure_cluster_role` re-sets the role password to the current secret on every
  bring-up, so a rotation self-heals — the cost of losing `.env` is credentials
  and config, not data.) The leftover home stays *un-bootable* instead: its
  `destroy-intent.json` is `detached`, which `ava init` and `ava start` both refuse
  (`cli/start_identity.py`).
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
  `ava status` and end it with `ava start`
  ([graceful maintenance](../../docs/conventions/operations/graceful-maintenance.md)).

## Key dependencies

- [[cli.ava.okf.md]] — the CLI overview: why identity is path-only, and the top-level verbs
- [[cli/commands/docs/commands.ava.okf.md]] — the module split these handlers live in
