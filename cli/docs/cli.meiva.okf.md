---
type: doc
title: CLI
description: 'Settings-free syntax and home admission dispatch lifecycle and operator commands.'
tags:
- gateway
- tool
- cluster-lifecycle
---

# CLI

`cli.parsers` owns settings-free syntax. `parse_args` preserves argparse
defaults, help and error exits without dispatch; `build_parser` composes handler
bindings; `command_options` gives doc checks command-scoped long options and
aliases. Parsing preserves caller-owned children. Agents/notices
and impersonation builders live in `cli.commands.agents`, extensions in
`cli.commands.extensions.parsers`, others in `cli/parsers/`. Adapters lazy-import
`cmd_*` at dispatch; `pyproject.toml` installs `.venv/bin/meiva` via `uv sync`.
Operator inputs are described in [[cli/database/docs/database.meiva.okf.md]].

## Top-Level Commands

### Cluster Lifecycle
| Command | Function |
|---------|----------|
| `meiva init` | Once per home, Settings-free: records the machine name, capabilities, credentials, ports and (for a runner) the gateway join, publishes `.env`, and starts nothing. An interrupted init resumes with no flags; an initialized home is refused |
| `meiva start` | Idempotent first and repeated startup of an initialized home; it takes only the service selection and refuses a home `meiva init` has not finished. A Settings-free admission precedes host convergence, owned storage/schema provisioning (first start), root launch, and all-selected-service readiness. Exit 0 means ready, 4 means readiness failed, and 1 means a step failed |
| `meiva stop` | Same drain, then full local stop including terminals, browser, extras and private pg/redis; `--keep-infra` / repeatable `--keep-service` preserve selected resources; `--force` is explicit |
| `meiva restart` | Stop + start on this unit: services are replaced and terminals close; the browser and infrastructure stay up |
| `meiva status` | status (including the maintenance hold, pg/redis and the end-to-end private-network Redis bridge view); `meiva status --json` prints only the hold as one JSON object, settings-lite |
| `meiva converge` | replays idempotent host wiring (symlink/PATH/dirs/plugin images and the macOS Redis bridge), usually via `meiva start`; it never touches the memory pool |
| `meiva firewall status` / `meiva firewall sync` | macOS Application Firewall allowlist manifest: `status` renders each manifest purpose, glob, resolved path, and Allow/Block/Missing state; `sync` applies it (repair + prune stale rules). Unprivileged mutation was empirically verified on the macmini running macOS 15.3.1, then falls back to non-interactive `sudo -n` and finally reports the exact manual commands on platforms that still require elevation |

### `meiva cluster` Subcommands

Verbs that act on the cluster rather than on this host's services, on the home
`MEIVA_HOME` names (else `~/.meiva`): `status` / `destroy` / `health-probe` /
`cron-*` / other registered commands. Enumerated in [[cli/docs/cluster.meiva.okf.md]].

### Agent & Ops

Agent lifecycle, context reads, local operations, and package-management command
groups are enumerated in [[cli/docs/operator-surfaces.meiva.okf.md]].

Ordinary `meiva start` resumes the existing local maintenance hold after readiness. Durable
agent identity and work survive both stop and restart; live terminal processes
survive neither. See [operator procedure](../../docs/conventions/operations/graceful-maintenance.md).

## Init and idempotent start

`cli/init_intent.py` validates the first-start inputs before Settings loads, and
`cli/start_identity.py` durably records the home, capabilities, credentials, and the
port table under the home lifecycle lock; init stops there. `cli/start_intent.py`
admits an initialized home for `meiva start` (Settings-free, no side effects), holds
the lock through readiness, and a bare repeated start preserves identity and
desired service selection. A home never initialized, one whose init was
interrupted, unknown existing resources, and a destroyed home refuse a start; an
initialized home refuses an init. The contract is in
[[cli/docs/start_identity.meiva.okf.md]].

A pure runner joins through `meiva init --serve-agent-runner --no-serve-gateway`
with `--gateway-url`, `--machine-name`, `--machine-host`, an environment bearer
and `--db-capability` (the unit capability from `meiva cluster db-authority
issue-unit`, [[base/cluster/authority/docs/wiring.meiva.okf.md]]). It installs it
before persisting identity, creating no local data plane; a later bundle goes to
`meiva cluster db-authority install-unit` (`cli/commands/cluster/unit_join.py` is the one join).
Every runner process fetches current connection facts at Settings construction.

Package acquisition is separate and has no cluster effects:
[[cli/docs/python-install.meiva.okf.md]].

## Internal Commands (`_` prefix)

Per-cluster pg/redis bring-up, host convergence, the host lifecycle and the
`_`-prefixed helpers they share are enumerated in
[[cli/commands/docs/commands.meiva.okf.md]].

## Design Principles

- **Path-only cluster identity**: identity **is** the home path — no name. Registry home-keyed; verbs address via `--path`. A checkout's `meiva` acts on its home's cluster (`cli/commands/_repo.py`).
- **Two lifecycle entries**: `meiva init` owns identity, `meiva start` owns startup and restart; package acquisition does not create cluster identity or launch services.
- **Ops-layer only**: not exposed to agents (they use the `meiva.*` SDK).
- **Settings-independent**: `meiva init` and `meiva start`'s admission are specially routed in `main()` before settings-gated imports — no `base.config` (stdlib + `base.host.env.dotenv_boot`). `meiva config` uses only registry metadata and direct local files until a full Settings consumer needs the singleton, so a broken `.env` remains repairable. `meiva pty` is settings-lite and data-plane-independent.
- **Cold stop**: normal stop loads the cluster configuration for native drain. Explicit force stop, or repeating a completed stop with no recorded failures, can skip gateway configuration fetch; the latter reads the existing pause journal before Settings bootstrap.
- **Migrations are not a command**: `cli/commands/lifecycle/migrations.py:cmd_migrations_apply` runs internally from `meiva start`.

## Entry Points

See [[entry.meiva.okf.md|fresh executable and embedded entry points]].

## Notes

- Bare `meiva` = `~/.local/bin/meiva`, a plain link to the production checkout's `.venv/bin/meiva` that converge maintains; it acts on `MEIVA_HOME`, else `~/.meiva`, like every CLI. A checkout's own `.venv/bin/meiva` runs that checkout's code. A home that carries its own `<home>/source` is operated only by that checkout's CLI: `cli.preflight.require_own_checkout` refuses every command from any other checkout, before anything loads the home (an argv of nothing, or a lone `-h`/`--help`, only parses and passes).
- Each cluster has its own pg/redis; isolation is home-directory isolation (instances under `$MEIVA_HOME` on the fixed port table), not db names / redis indexes in a shared instance.
- Children: [[cli/docs/cluster.meiva.okf.md]] (the `meiva cluster` verb group) · [[cli/docs/start_identity.meiva.okf.md]] (idempotent cluster start) · [[cli/commands/docs/commands.meiva.okf.md]] (the module split) · [[cli/commands/extensions/packages/docs/packages.meiva.okf.md]] (the plugins / skill / mcp package surface).
