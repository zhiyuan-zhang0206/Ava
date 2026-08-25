---
type: doc
title: CLI
description: '`ava` CLI — single entry point for cluster lifecycle; argparse dispatches to `cli/commands/`. Cluster identity = home path: born at install time, `ava start` pure bring-up; each cluster has its own pg/redis.'
tags:
- gateway
- tool
- cluster-lifecycle
---

# CLI

The `ava` CLI — single entry point for cluster lifecycle. `cli/main.py` builds the argparse tree via `cli/parsers/` (one module per command domain, each holding its subcommand builders + `_h_*` handlers) and dispatches to the `cmd_*` implementations under `cli/commands/`; registered in `pyproject.toml [project.scripts]`, available as `.venv/bin/ava` after `uv sync`.

## Top-Level Commands

### Cluster Lifecycle
| Command | Function |
|---------|----------|
| `ava start` | **Pure bring-up** — birth moved to install time (`scripts/install.sh`); settings-free `cli.preflight.require_installed_home` fails uninstalled homes fast (→ install.sh). Ensures the cluster's own pg/redis + starts the union of local services. **Exit code carries readiness**: 0 = serving, 4 (`SERVICES_NOT_READY_EXIT_CODE`) = every step ran but a launched service never passed its probe (snapshot printed + sessions named first), 1 = a step failed. `--no-readiness-gate` drops the code, not the diagnosis — used by the boot job (uncapped retry) and the rollout's local leg (`_gateway_ready` answers it better) |
| `ava stop` | stdin-confirmed force kill (local services + cluster pg/redis); keeps logged-in Chrome unless `--stop-browser` |
| `ava restart` | stop (force, unconfirmed) + start — updater path (single-host; cross-host bounce: `ava cluster restart`) |
| `ava status` | status (incl. pg/redis view) |
| `ava cluster update` | capability-dispatched: gateway-capable orchestrates the cluster; pure agent-runner self-updates |
| `ava converge` | replays idempotent host wiring (symlink/PATH/dirs/plugin images/memory pool), usually via `ava start` |
| `ava firewall status` / `ava firewall sync` | macOS Application Firewall allowlist manifest: `status` renders each manifest purpose, glob, resolved path, and Allow/Block/Missing state; `sync` applies it (repair + prune stale rules). Unprivileged mutation was empirically verified on the macmini running macOS 15.3.1, then falls back to non-interactive `sudo -n` and finally reports the exact manual commands on platforms that still require elevation |
| `ava enroll` | registers a remote agent-runner (presents `AVA_CLUSTER_SECRET` on **GET** `/api/bootstrap`; the compatibility `--cluster-secret` flag remains, but the environment keeps it out of argv), verifies the runner credential projection, and atomically writes only the owner-readable bootstrap identity/reachability env. Connection facts are not cached: every runner process re-fetches them at startup. `--health-port-base` states this UNIT's own daemon health ports, which the gateway does not own. Omitted on WSL2, a fixed reserved base is auto-applied instead of the shared default (issue #1152); a base already in `.env` survives a bare re-enroll |
| `ava boot` | what the OS boot job runs on platforms whose scheduler cannot retry a failed job (Linux cron / Windows schtasks); uncapped retry of `ava start` |

### `ava cluster` Subcommands

Verbs that act on a cluster rather than on this host's services, addressed by
**home path** (`--path`), not name: `ls` / `status` / `restart` / `down` /
`destroy` / `rollback` / `health-probe` / `recover` / `cron-*` /
`watchdog-probe`. Enumerated in [[cli/cluster.ava.okf.md]].

### Agent & Ops
`ava agents ls/send/cancel/restart/resurrect/terminate/kill` · `ava config get/set/unset` (merge patch, not full-replace) · `ava logs retention` (local, non-recursive managed-log cleanup; legacy global 14-day fallback or explicit family tiers; open handles excluded) · `ava mcp ...` (isolated venvs at `$AVA_HOME/mcps/`; plus `ava mcp serve`, which runs the other way and exposes this cluster's control plane as an MCP server — [[ava/mcps.ava.okf.md|MCP]]) · `ava memory refresh` (the only `ava memory` verb; pool consolidation runs on the gateway schedule — `schedules/memory-steward-schedule.py` → `ava_builtins/plugins/ava_memory/skills/scripts/{steward,arbiter_merge}.py`) · `ava plugins ...` · `ava skill install/update/upgrade/enable/disable/register/scan/trust` · `ava presets ls/get/create/update/delete` · `ava schedules ls/get/create/update/delete/provision/start/stop/restart/logs/runs` (provision = create the built-in schedules from `schedules/manifest.json`; runs at gateway boot too) · `ava trace ship` · `ava lgtm on/off/status` (observability-stack toggle on this host — marker + native lifecycle)

## Install-Time Birth (`cli/install_cluster.py`)

`scripts/install.sh`'s final step runs `python -m cli.install_cluster` — the
only path that births a cluster (registry entry keyed by home path + own
pg/redis + provisioned database + `$AVA_HOME/.env`). Flags (`--role`,
`--worktree`, `--seed`) and the data-plane identity rule are in
[[cli/install_cluster.ava.okf.md]].

## Internal Commands (`_` prefix, run by start/update)

Per-cluster pg/redis bring-up, host convergence, the staged upgrade legs, and
the rest of the `_`-prefixed steps are enumerated in
[[cli/commands/commands.ava.okf.md]].

## Design Principles

- **Path-only cluster identity**: identity **is** the home path — no name. Registry home-keyed (legacy name-keyed records read compatibly); verbs address via `--path`. A checkout's `ava` acts on its home's cluster (`cli/commands/_repo.py`).
- **install births, `ava start` pure bring-up**: birth only at install; preflight rejects uninstalled homes, never implicit birthing.
- **Ops-layer only**: not exposed to agents (they use the `ava.*` SDK).
- **Settings-independent**: `ava enroll` / the `ava start` preflight are specially routed in `main()` before settings-gated imports — no `shared.config` (stdlib + `shared.dotenv_boot`).
- **Migrations are not a command**: `cli/commands/migrations.py:cmd_migrations_apply` runs internally from `ava start` / `ava cluster update`.

## Entry Points

- `cli/main.py:main()` — argparse entrypoint + enroll/preflight special routing; `cli/parsers/` — the settings-free argparse tree (builders + handlers per domain); `cli/preflight.py:require_installed_home()` — settings-free installed-home gate
- `cli/install_cluster.py:cmd_install_cluster()` — install-time birth; `cli/enroll.py:run_enroll` — config-free enrollment
- `cli/commands/cluster_lifecycle.py` — registry allocation + `ls/down/destroy` (`--path` addressed); `start.py` / `status.py` / `_cluster_instance.py`

## Notes

- Prod `ava` = `~/.local/bin/ava` → symlink to the prod checkout, acting on `~/.ava`; in a dev worktree, `.venv/bin/ava` acts on that worktree's cluster (via the `.ava_home` pointer).
- Each cluster has its own pg/redis; isolation is home-directory isolation (instances under `$AVA_HOME` + port blocks), not db names / redis indexes in a shared instance.
- Children: [[cli/cluster.ava.okf.md]] (the `ava cluster` verb group) · [[cli/install_cluster.ava.okf.md]] (install-time birth) · [[cli/commands/commands.ava.okf.md]] (the module split) · [[cli/commands/packages.ava.okf.md]] (the plugins / skill / mcp package surface) · [[cli/mcp_server.ava.okf.md]] (`ava mcp serve` — this cluster AS an MCP server).
