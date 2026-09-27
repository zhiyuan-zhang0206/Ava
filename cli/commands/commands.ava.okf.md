---
type: doc
title: CLI Command Modules
description: One module per `ava` subcommand, plus `_`-prefixed internal steps that only start / update call. Public modules define `cmd_*` handlers, wired by the argparse tree in `cli/main.py`.
tags:
- cli
- tool
---

# CLI Command Modules

## What it is

`cli/commands/` holds one module per `ava` subcommand. The argparse tree lives
in `cli/parsers/` (one module per command domain, each holding that domain's
subcommand builders and `_h_*` handlers); `cli/main.py` composes it and
dispatches via `set_defaults(func=)` to the module's `cmd_*` handler — there is
no registry or plugin mechanism, the wiring is the parser.

`cli/commands/__init__.py` is the command door: it imports the `cmd_*` names
`cli.parsers` handlers lazy-import from the door (a few parsers instead import
a domain module directly, e.g. `cli.parsers.agents` reads `cmd_agents_ls` from
`cli.commands.agents.control`). Six subpackages hold the leaf domains split out
of the once-flat directory, each an independent package door:

- `agents/` — lifecycle control, notices, timelines, external-agent
  impersonation, the pty/computer-use daemons
- `management/` — gateway-managed config, presets, schedules
- `extensions/` — plugins, skills, packages, MCP servers, memory
- `observability/` — native LGTM, the OTel collector, trace shipping, logs
- `data_plane/` — per-cluster Postgres/Redis/PgBouncer, db roles, PITR
- `cluster/` — whole-cluster verbs, the health probe, watchdogs, the registry

Cross-version process entry points — run as `python -m cli.commands.X` by the
ops server against a possibly different checkout — stay at the root and never
move: `_update_agent_runner`, `_updater_stage`, `_updater_lease`,
`_update_uv_sync`, `_installed_sha`, `_source_switch_marker`, `_hold_recover`,
`_update_pitr`. The `_converge*` / `_update*` step families, `start.py` /
`stop.py` / `status.py` / `update.py` / `migrations.py`, and the rest of the
not-yet-split leaves stay directly under `cli/commands/` for now.

`stop.py` exposes `pause` and `stop` through `_temporary_stop`; update and
restart reuse its native drain. `ops.agent_pause` and `ops.agent_pause_probe`
own prepare/drain and runtime capability checks; `_maintenance_stop` and
`_maintenance_data_plane` verify resource exits — the data-plane stop signals
the pooler with SIGINT (`WAIT_FOR_SERVERS`) and stops Postgres with
`pg_ctl -m fast`, so neither waits on idle client connections a drained state
cannot protect (issue #2307). When the data-plane phase still fails after the
services phase stopped, `_temporary_stop` compensates with a bounded internal
`ava start` (restoring the services and reviving a half-shut pooler) instead of
leaving the unit dark; the stop report and journal record the outcome (issue
#2307). `_stop_extras` and
`_stop_supervised` stop home-owned Gate/helper/native LGTM. `_pause_resume`
releases normal startup admission only after readiness.
`cli/parsers/maintenance.py` retains explicit intermediate steps through
`cli/commands/maintenance.py` and `_maintenance_probe`.
They reuse the [durable maintenance journal](../../shared/maintenance/maintenance.ava.okf.md).
See [the coordinated operator procedure](../../conventions/graceful-maintenance.md).

Gateway data-plane startup passes separate URL identities to `cluster_instance`:
Postgres db/role comes from `db_identity()`, Redis ACL user from `redis_identity()`.
`admin_secrets` preserves that distinction during credential splitting.
Installation supplies the same birth identifier for both before `.env` exists.
Legacy username backfill adopts its committed Redis URL in the same start
process, including named `nopass` URLs for no-auth homes.

`cli/commands/migrations.py:cmd_migrations_apply` is deliberately not a user-facing verb —
it runs as a step of `ava start` / `ava update`, so any restart crossing a
schema change catches the DB up on its own.

## Notes

- The fleet UI gate stays outside service-session teardown. Linux uses a
  per-home user-systemd unit with crash restart; unchanged active units survive
  updates, while source-hash changes replace them after a completed stop.
  Full stop and destroy use that same home identity. See
  [Linux gate supervision](../../conventions/linux-gate-supervision.md).

- `agents/timeline.py` exposes the existing timeline API as `ava agents timeline`
  and its exact `context` alias. `agents/impersonation.py` manages explicit external
  requests, leases, inbox acknowledgments, local Python SDK attachment, and the
  one attested send (`send` — task #4102).
  `agents/impersonation_relay.py` forwards inbound availability to the owning external
  model session; `--codex-remote` routes to the app server holding a Codex thread
  without waiting for its external queue-store scan.
  Usage: [External agent impersonation](../../conventions/agent-impersonation.md).

- Which cluster a command acts on comes from `cli/commands/_repo.py:_repo_root` — the
  checkout the running `ava` belongs to — never the current directory.
- What `ava start` treats as already-up, what it waits for, and when an unready
  service becomes exit code 4 are one subject, in [[start-readiness.ava.okf.md]].
- The gateway/runner update boundary, readiness proof, Phase-B verdicts, and
  failed-update recovery are one subject in [[rollout-boundary.ava.okf.md]].
- The restricted immutable `ava-ops` hop is [[update-bootstrap.ava.okf.md]];
  its sealed normal-service planning contract and disabled activation boundary are
  [[normal-release.ava.okf.md]].
- A full agent-runner update checks out, syncs, and records the installed SHA in
  its pre-checkout image, then re-execs `_update_agent_runner` with its private
  post-checkout flags before validation, quiesce, stop, or start. Persistent
  schedule terminals are retained, including their currently loaded code;
  an explicit schedule restart or full stop/start adopts new runner code.
- Host-level Application Firewall and Redis bridge wiring are one subject:
  [[converge-host-wiring.ava.okf.md]].
- The prod editable-install assertion and update write window are one lifecycle
  guard: [[editable-install-guard.ava.okf.md]]; the prod source checkout's
  integrity (periodic reset + probe detection) is its sibling guard:
  [[source-tree-guard.ava.okf.md]].
- `cli/enroll.py` and `cli/preflight.py` are routed **before** settings-gated
  imports in `main()`, so they work on a host with no usable config yet.
- `cli/mcp_server.py` is the third top-level module a verb routes to
  (`ava mcp serve`) rather than a `commands/` module: it is a long-running
  stdio server, not a command that renders and exits, and it pulls in the mcp
  SDK that no other verb needs. See [[cli/commands/extensions/packages.ava.okf.md]].
- [[cli/commands/data_plane/pitr.ava.okf.md]] defines the PITR inspection surface and the archive →
  verify → retire guard for finite migration rollback snapshots.
- [[ownership_preflight.ava.okf.md]] names the warning-only ownership repair
  guard that runs before converge writes later host state.

## Key Dependencies

- [[cli.ava.okf.md]] — the CLI domain overview: verbs, cluster identity, install-time birth
- [[packages.ava.okf.md]] — the `ava plugins` / `ava skill` / `ava mcp` package surface
- [[start-readiness.ava.okf.md]] — what `ava start` calls up: the launch guard, the
  readiness wait, and the waiver over its exit code
- [[rollout-boundary.ava.okf.md]] — rollout child classification, gateway
  readiness, Phase B, and recovery authentication
