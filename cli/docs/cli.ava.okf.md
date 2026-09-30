---
type: doc
title: CLI
description: '`ava start` owns durable home identity, native provisioning, and root-owned readiness; argparse dispatches lifecycle operations to cli/commands/.'
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
| `ava start` | Idempotent first, repeated, and interrupted startup. Settings-free identity admission precedes host convergence, owned storage/schema provisioning, root launch, and all-selected-service readiness. Exit 0 means ready, 4 means readiness failed, and 1 means a step failed. `--worktree` selects an isolated dev home; first-start capabilities and runner join inputs use this same entry |
| `ava pause` | Normal native drain and service stop; retain infrastructure, browser and persistent PTYs. Default timeout 300 seconds, no implicit force |
| `ava stop` | Same drain, then full local stop including terminals, browser, extras and private pg/redis; `--keep-infra` / repeatable `--keep-service` preserve selected resources; `--force` is explicit |
| `ava restart` | Pause + start on this unit, retaining PTYs and infrastructure |
| `ava status` | status (including pg/redis and the end-to-end private-network Redis bridge view) |
| `ava converge` | replays idempotent host wiring (symlink/PATH/dirs/plugin images and the macOS Redis bridge), usually via `ava start`; it never touches the memory pool |
| `ava firewall status` / `ava firewall sync` | macOS Application Firewall allowlist manifest: `status` renders each manifest purpose, glob, resolved path, and Allow/Block/Missing state; `sync` applies it (repair + prune stale rules). Unprivileged mutation was empirically verified on the macmini running macOS 15.3.1, then falls back to non-interactive `sudo -n` and finally reports the exact manual commands on platforms that still require elevation |
| `ava boot` | manual uncapped retry of `ava start` while boot dependencies become available |

### `ava cluster` Subcommands

Verbs that act on a cluster rather than on this host's services, addressed by
**home path** (`--path`), not name: `status` / `down` /
`destroy` / `health-probe` / `recover` / `cron-*` /
other registered commands. Enumerated in [[cli/docs/cluster.ava.okf.md]].

### Agent & Ops

Agent lifecycle, context reads, local operations, and package-management command
groups are enumerated in [[cli/docs/operator-surfaces.ava.okf.md]].

Ordinary `ava start` resumes the existing local pause after readiness. Durable
agent identity and work survive both pause and stop; live terminal processes
survive pause only. See [operator procedure](../../conventions/graceful-maintenance.md).

## Idempotent start

`cli/start_intent.py` validates identity before Settings loads and holds the home
lifecycle lock through readiness. `cli/start_identity.py` durably records the
home, capabilities, credentials, and allocated ports before their first effects.
An interrupted first start resumes that intent; a bare repeated start preserves
identity and desired service selection. Unknown existing resources, conflicting
inputs, and a destroyed home refuse. The contract is in
[[cli/docs/start_identity.ava.okf.md]].

Operation-owned image startup uses this same identity-before-configuration
boundary. The stage carries only a settings-free journal capability; the common
start guard binds its exact maintenance holder and timestamp after preparation.
It refuses missing, changed, or unsettled holds and leaves resume to the finite
executor after observation. Ordinary startup keeps its readiness-gated resume.

A pure runner joins through `ava start --serve-agent-runner --no-serve-gateway`
with `--gateway-url`, `--machine-name`, `--machine-host`, an environment bearer
and `--db-capability` (the unit capability from `ava cluster db-authority
issue-unit`, [[base/cluster/authority/docs/wiring.ava.okf.md]]). It installs it
before persisting identity, creating no local data plane. Every runner process
fetches current connection facts at Settings construction.

Package acquisition is separate and has no cluster effects:
[[cli/docs/python-install.ava.okf.md]].

The inactive committed application build input is described in
[[cli/docs/release-build.ava.okf.md]].

## Internal Commands (`_` prefix)

Per-cluster pg/redis bring-up, host convergence, the host lifecycle and the
`_`-prefixed helpers they share are enumerated in
[[cli/commands/docs/commands.ava.okf.md]].

## Design Principles

- **Path-only cluster identity**: identity **is** the home path — no name. Registry home-keyed (legacy name-keyed records read compatibly); verbs address via `--path`. A checkout's `ava` acts on its home's cluster (`cli/commands/_repo.py`).
- **One lifecycle entry**: `ava start` owns initialization and restart; package acquisition does not create cluster identity or launch services.
- **Ops-layer only**: not exposed to agents (they use the `ava.*` SDK).
- **Settings-independent**: `ava start` identity admission is specially routed in `main()` before settings-gated imports — no `base.config` (stdlib + `base.host.env.dotenv_boot`). `ava config` uses only registry metadata and direct local files until a full Settings consumer needs the singleton, so a broken `.env` remains repairable. `ava pty` is settings-lite and data-plane-independent.
- **Cold stop**: normal pause/stop loads the cluster configuration for native drain. Explicit force stop, or repeating a completed stop with no recorded failures, can skip gateway configuration fetch; the latter reads the existing pause journal before Settings bootstrap.
- **Migrations are not a command**: `cli/commands/lifecycle/migrations.py:cmd_migrations_apply` runs internally from `ava start`.

## Entry Points

- `cli/main.py:main()` — argparse entrypoint; `cli/parsers/` — the settings-free command tree.
- `cli/start_intent.py:run_start()` — first-start inputs and full home lifecycle lock; `cli/start_identity.py` — durable initialization journal.
- `cli/commands/cluster/home.py` — `down/destroy` (`--path` addressed), exact cleanup before marking the home detached; `start.py` / `status.py` / `data_plane/cluster_instance.py` — runtime operations.

## Notes

- Bare `ava` = `~/.local/bin/ava` → `scripts/ava-launcher.sh`: runs `$AVA_HOME/ava` (the home's CLI link) or refuses; a checkout's `.venv/bin/ava` acts on its own cluster (`.ava_home` pointer).
- Each cluster has its own pg/redis; isolation is home-directory isolation (instances under `$AVA_HOME` + port blocks), not db names / redis indexes in a shared instance.
- Children: [[cli/docs/cluster.ava.okf.md]] (the `ava cluster` verb group) · [[cli/docs/start_identity.ava.okf.md]] (idempotent cluster start) · [[cli/commands/docs/commands.ava.okf.md]] (the module split) · [[cli/commands/extensions/docs/packages.ava.okf.md]] (the plugins / skill / mcp package surface) · [[cli/docs/mcp_server.ava.okf.md]] (`ava mcp serve` — this cluster AS an MCP server).
