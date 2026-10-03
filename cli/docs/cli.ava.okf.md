---
type: doc
title: CLI
description: '`ava init` records a home''s identity once; `ava start` owns native provisioning and root-owned readiness; argparse dispatches lifecycle operations to cli/commands/.'
tags:
- gateway
- tool
- cluster-lifecycle
---

# CLI

The `ava` CLI — single entry point for cluster lifecycle. `cli/main.py` builds the argparse tree via `cli/parsers/` (one module per command domain, each holding its subcommand builders + `_h_*` handlers) and dispatches directly to the `cmd_*` definitions under `cli/commands/` (the package marker exports no commands); registered in `pyproject.toml [project.scripts]`, available as `.venv/bin/ava` after `uv sync`.

## Top-Level Commands

### Cluster Lifecycle
| Command | Function |
|---------|----------|
| `ava init` | Once per home, Settings-free: records the machine name, capabilities, credentials, ports and (for a runner) the gateway join, publishes `.env`, and starts nothing. An interrupted init resumes with no flags; an initialized home is refused |
| `ava start` | Idempotent first and repeated startup of an initialized home; it takes only the service selection and refuses a home `ava init` has not finished. A Settings-free admission precedes host convergence, owned storage/schema provisioning (first start), root launch, and all-selected-service readiness. Exit 0 means ready, 4 means readiness failed, and 1 means a step failed |
| `ava stop` | Same drain, then full local stop including terminals, browser, extras and private pg/redis; `--keep-infra` / repeatable `--keep-service` preserve selected resources; `--force` is explicit |
| `ava restart` | Stop + start on this unit: services are replaced and terminals close; the browser and infrastructure stay up |
| `ava status` | status (including pg/redis and the end-to-end private-network Redis bridge view) |
| `ava converge` | replays idempotent host wiring (symlink/PATH/dirs/plugin images and the macOS Redis bridge), usually via `ava start`; it never touches the memory pool |
| `ava firewall status` / `ava firewall sync` | macOS Application Firewall allowlist manifest: `status` renders each manifest purpose, glob, resolved path, and Allow/Block/Missing state; `sync` applies it (repair + prune stale rules). Unprivileged mutation was empirically verified on the macmini running macOS 15.3.1, then falls back to non-interactive `sudo -n` and finally reports the exact manual commands on platforms that still require elevation |
| `ava boot` | manual uncapped retry of `ava start` while boot dependencies become available |

### `ava cluster` Subcommands

Verbs that act on the cluster rather than on this host's services, on the home
`AVA_HOME` names (else `~/.ava`): `status` / `destroy` / `health-probe` /
`cron-*` / other registered commands. Enumerated in [[cli/docs/cluster.ava.okf.md]].

### Agent & Ops

Agent lifecycle, context reads, local operations, and package-management command
groups are enumerated in [[cli/docs/operator-surfaces.ava.okf.md]].

Ordinary `ava start` resumes the existing local maintenance hold after readiness. Durable
agent identity and work survive both stop and restart; live terminal processes
survive neither. See [operator procedure](../../conventions/graceful-maintenance.md).

## Init and idempotent start

`cli/init_intent.py` validates the first-start inputs before Settings loads, and
`cli/start_identity.py` durably records the home, capabilities, credentials, and the
port table under the home lifecycle lock; init stops there. `cli/start_intent.py`
admits an initialized home for `ava start` (Settings-free, no side effects), holds
the lock through readiness, and a bare repeated start preserves identity and
desired service selection. A home never initialized, one whose init was
interrupted, unknown existing resources, and a destroyed home refuse a start; an
initialized home refuses an init. The contract is in
[[cli/docs/start_identity.ava.okf.md]].

A pure runner joins through `ava init --serve-agent-runner --no-serve-gateway`
with `--gateway-url`, `--machine-name`, `--machine-host`, an environment bearer
and `--db-capability` (the unit capability from `ava cluster db-authority
issue-unit`, [[base/cluster/authority/docs/wiring.ava.okf.md]]). It installs it
before persisting identity, creating no local data plane; a later bundle goes to
`ava cluster db-authority install-unit` (`cli/unit_join.py` is the one join).
Every runner process fetches current connection facts at Settings construction.

Package acquisition is separate and has no cluster effects:
[[cli/docs/python-install.ava.okf.md]].

## Internal Commands (`_` prefix)

Per-cluster pg/redis bring-up, host convergence, the host lifecycle and the
`_`-prefixed helpers they share are enumerated in
[[cli/commands/docs/commands.ava.okf.md]].

## Design Principles

- **Path-only cluster identity**: identity **is** the home path — no name. Registry home-keyed (legacy name-keyed records read compatibly); verbs address via `--path`. A checkout's `ava` acts on its home's cluster (`cli/commands/_repo.py`).
- **Two lifecycle entries**: `ava init` owns identity, `ava start` owns startup and restart; package acquisition does not create cluster identity or launch services.
- **Ops-layer only**: not exposed to agents (they use the `ava.*` SDK).
- **Settings-independent**: `ava init` and `ava start`'s admission are specially routed in `main()` before settings-gated imports — no `base.config` (stdlib + `base.host.env.dotenv_boot`). `ava config` uses only registry metadata and direct local files until a full Settings consumer needs the singleton, so a broken `.env` remains repairable. `ava pty` is settings-lite and data-plane-independent.
- **Cold stop**: normal stop loads the cluster configuration for native drain. Explicit force stop, or repeating a completed stop with no recorded failures, can skip gateway configuration fetch; the latter reads the existing pause journal before Settings bootstrap.
- **Migrations are not a command**: `cli/commands/lifecycle/migrations.py:cmd_migrations_apply` runs internally from `ava start`.

## Entry Points

- `cli/main.py:main()` — argparse entrypoint; `cli/parsers/` — the settings-free command tree.
- `cli/init_intent.py:run_init()` — the first-start inputs; `cli/start_intent.py:run_start()` — start admission and the full home lifecycle lock; `cli/start_identity.py` — durable initialization journal and the start gate (`require_initialized`); `cli/unit_join.py` — a runner's gateway join.
- `cli/commands/cluster/home.py` — `destroy` (confirmed at a terminal), exact cleanup before marking the home detached; `start.py` / `status.py` / `data_plane/cluster_instance.py` — runtime operations.

## Notes

- Bare `ava` = `~/.local/bin/ava`, a plain link to the production checkout's `.venv/bin/ava` that converge maintains; it acts on `AVA_HOME`, else `~/.ava`, like every CLI. A checkout's own `.venv/bin/ava` runs that checkout's code. A home that carries its own `<home>/source` is operated only by that checkout's CLI: `cli.preflight.require_own_checkout` refuses every command from any other checkout, before anything loads the home (an argv of nothing, or a lone `-h`/`--help`, only parses and passes).
- Each cluster has its own pg/redis; isolation is home-directory isolation (instances under `$AVA_HOME` on the fixed port table), not db names / redis indexes in a shared instance.
- Children: [[cli/docs/cluster.ava.okf.md]] (the `ava cluster` verb group) · [[cli/docs/start_identity.ava.okf.md]] (idempotent cluster start) · [[cli/commands/docs/commands.ava.okf.md]] (the module split) · [[cli/commands/extensions/docs/packages.ava.okf.md]] (the plugins / skill / mcp package surface).
