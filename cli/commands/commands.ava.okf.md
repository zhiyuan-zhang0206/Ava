---
type: doc
title: CLI Command Modules
description: One subpackage per `ava` command domain, plus the few `_`-prefixed helpers those domains share. Public modules define `cmd_*` handlers, wired by the argparse tree in `cli/main.py`.
tags:
- cli
- tool
---

# CLI Command Modules

## What it is

`cli/commands/` holds the `ava` command domains. The argparse tree lives
in `cli/parsers/` (one module per command domain, each holding that domain's
subcommand builders and `_h_*` handlers); `cli/main.py` composes it and
dispatches via `set_defaults(func=)` to the module's `cmd_*` handler — there is
no registry or plugin mechanism, the wiring is the parser.

`cli/commands/__init__.py` is an empty package door: no import work and no
re-exports, so `import cli.commands` loads nothing else, Settings included.
Each command module is its own door; `cli.parsers` handlers lazy-import
`cmd_*` from the module that defines it, and test seams patch there. Eight
subpackages hold the domains, each an independent package door:

- `agents/` — lifecycle control, notices, timelines, external-agent
  impersonation, the pty/computer-use daemons
- `management/` — gateway-managed config, presets, schedules
- `extensions/` — plugins, skills, packages, MCP servers, memory; owns its
  converge steps (`materialize.py`, `skills_sync.py`, `external_skills.py`)
- `observability/` — native LGTM desired state, the OTel collector, trace
  shipping, logs; owns its converge steps (`lgtm_native.py`, `otel_collector.py`)
- `data_plane/` — per-cluster Postgres/Redis/PgBouncer bring-up, their verified
  maintenance stop (`maintenance_stop.py`), a release's write-generation
  effects (`write_generation.py`), PITR; owns its converge steps
  (`pgbouncer.py`, `pitr_foundation.py`)
- `cluster/` — whole-cluster verbs, the health probe, cron, the registry
- `converge/` — the orchestrator (`host.py`), the step contract (`spec.py`),
  the warning-only start preflights (health, ports, ownership), the
  rendered-file guard, and host-wiring steps owned by no other domain
  (firewall, Redis bridge, OS jobs).
- `lifecycle/` — `ava start` / `pause` / `stop` / `restart` / `maintenance` /
  `status`, the pending-migration step, and the single application root they
  drive: [[cli/commands/lifecycle/lifecycle.ava.okf.md|Host lifecycle]].

The package root keeps only what several domains share or what must not move:
`_repo` (the checkout root, capability read and roster façade), `_setup`
(first-start field resolution), `_probe` (identity-bound service diagnostics,
read by `lifecycle/` and the cluster health probe), `release_inventory.py`
([[release-inventory.ava.okf.md]]) and `_release_plugin_probe.py`. The last is a
cross-version contract: `shared/deploy/release/runtime_prepare.py` names it in a `-c` program
that the candidate image's own interpreter runs, so a preparer of one version
imports it from an image of another; it stays at this path.

Gateway data-plane startup (`data_plane/cluster_instance`, `data_plane/bringup`,
`data_plane/pgbouncer`):
[[cli/commands/data_plane/data-plane-startup.ava.okf.md|Gateway data-plane startup]].

## Notes

- Gate is an ordinary gateway service selected into the root manifest. Planned
  root downtime includes its entry port; it has no separate OS job or detached
  launcher. See [[services/gate/gate.ava.okf.md|Fleet UI Gate]].

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
  service becomes exit code 4 are one subject, in
  [[cli/commands/lifecycle/start-readiness.ava.okf.md]].
- Prepared release transitions are owned by [[cli/release_transition/release_transition.ava.okf.md]].
  There are no updater shell chains or bootstrap/continuation commands.
- Host-level Application Firewall and Redis bridge wiring are one subject:
  [[converge/converge-host-wiring.ava.okf.md]].
- Explicit editable-install inspection and write-window primitives are described
  in [[editable-install-guard.ava.okf.md]]. Ordinary start and converge never
  repair or reinstall a separate production virtualenv; retained images are
  verified in place by `StartRuntime`.
- `cli/start_intent.py` is routed **before** settings-gated imports in `main()`,
  so first start records complete home identity before runtime configuration loads.
- `cli/mcp_server.py` is the third top-level module a verb routes to
  (`ava mcp serve`) rather than a `commands/` module: it is a long-running
  stdio server, not a command that renders and exits, and it pulls in the mcp
  SDK that no other verb needs. See [[cli/commands/extensions/packages.ava.okf.md]].
- [[cli/commands/data_plane/pitr.ava.okf.md]] defines the PITR inspection surface and the archive →
  verify → retire guard for finite migration rollback snapshots.
- [[cli/commands/converge/ownership_preflight.ava.okf.md]] names the
  warning-only ownership repair guard that runs before converge writes later
  host state.

## Key Dependencies

- [[cli.ava.okf.md]] — the CLI domain overview: verbs, cluster identity, idempotent first start
- [[packages.ava.okf.md]] — the `ava plugins` / `ava skill` / `ava mcp` package surface
- [[cli/commands/lifecycle/lifecycle.ava.okf.md]] — start, stop, pause,
  maintenance and the application root
