---
type: doc
title: "`$MEIVA_HOME` Layout"
description: '`base/paths/__init__.py` resolves every per-unit path from `$MEIVA_HOME`. One home = one unit; co-located units keep separate state because their homes differ. `meiva_home()` reads the environment and creates the home; a helper that takes `home` is a pure derivation that creates nothing.'
tags:
- base
- library
- filesystem
---

# `$MEIVA_HOME` Layout

## What it is

`base/paths/__init__.py` is the single-point resolver for every per-unit path. The root
comes from `base.paths.home.resolve_meiva_home` (env `MEIVA_HOME`, default
`~/.meiva`), read on every call — never captured at import. Only
`meiva_home()` reads the environment and creates the home (mode 0700); the composition
root of a process calls it once and passes the result down.

A helper takes one of two forms. The **pure form** `H(home)` (`traces_dir`, `skills_dir`,
`mcps_dir`, `install_registry_path`, `running_sha_path`, `launch_failures_path`, the run
tree — `run_dir`, `exec_run_dir`, `root_run_dir`, `root_manifests_path`, the chrome, computer,
permissions-helper, insights and MCP-daemon sockets, `insights_pidfile` — `quarantined_exec_requests_dir`
and the `otel_collector_*` paths) derives a path from the home it is handed: it reads no
environment and creates nothing, so a test hands in `tmp_path` and the process that binds a
socket, takes a lock or writes a file creates the directory. The zero-argument
helpers that remain read `MEIVA_HOME` and `mkdir(parents=True, exist_ok=True)` on first
access; each is replaced by its pure form together with its callers.

All three process classes — gateway, agent, exec subprocess — inherit the same
`MEIVA_HOME` variable, so nothing is passed by hand. **The home is the unit's
identity**: a co-located gateway unit at `~/.meiva_gateway` and a runner unit at
`~/.meiva` share no state, because every path below derives from a different root.

## Layout

```
$MEIVA_HOME/
├── .env                        # the unit's config source of truth
├── .env.lock                   # serializes .env rewrites across processes (never .env itself)
├── source/                     # prod only — the git checkout the sessions run from
├── pg/  redis/                 # this cluster's own data-plane instances
├── run/                        # pidfiles, unix sockets, sessions/ (native-supervisor records)
├── logs/                       # <daemon>.log, agent-{N}.out.log / .stderr.log, rollout-<ts>.log
├── traces/                     # spans.jsonl + rotated spans-<ISO>.jsonl (collector mirror) + .ship-watermark.json
├── otel-collector/             # otelcol-contrib binary + config.yaml + queue/ (sidecar, task #1266)
├── backups/db/                 # daily pg_dump --format=custom, UTC-stamped, newest 7 kept
├── backups/env/                # .env snapshot taken before each config write
├── memory/                     # the memory pool git repo
├── workspaces/<agent_id>/      # per-agent working dir
├── chrome-profile/             # the shared headed Chrome's dedicated profile
├── plugins_config.json         # per-machine plugin enable state
├── installed.json              # install registry (skills / plugins / MCP packages)
├── installed.json.lock         # serializes registry rewrites across processes (never the registry)
├── mcp.json  mcp_enabled.json  # machine MCP server defs + per-host enable overlay
├── skills/<name>/SKILL.md      # the single skill load dir (gated by the registry)
├── mcps/<name>/                # installed MCP packages, each with its own .venv
├── plugins/<name>/plugin.py    # externally installed plugins
├── disabled_services           # durable `--disable-service` set the watchdog honors
└── deploy-state.*.lock         # home lifecycle mutexes (+ .holder.json diagnostics)
```

There is no separate host state directory: the host runs one cluster, so what a
host shares lives in its home — the vendored Postgres runtime (`runtime/`), the
initdb template (`pg-template-17/`), coding-session owner records
(`coding-session-owners/`) and the PTY allocation gate (`pty-allocation-freeze.json`
+ its stable `pty-allocation.lock`). Pointing `MEIVA_HOME` at a temporary directory
therefore isolates all of them. The home lists no clusters: each home describes
only itself.

## Notes

- **`.env` / `installed.json` lock discipline** (sibling file locks at every door, leaves stay leaves, atomic save vs lost update): [[base/paths/docs/lock-discipline.meiva.okf.md]].
- The home is `MEIVA_HOME` when set, else `~/.meiva`
  (`base/host/env/dotenv_boot.py:resolve_meiva_home`), never from cwd and never from a
  flag: no pointer file, no checkout claim, no in-process override. A home that
  carries its own `<home>/source` checkout is operated only by that checkout's code
  (`base.paths.home.home_checkout_error`; `prod_service_checkout_error` applies it to
  service launches). Cluster identity **is** this path — there is no cluster name;
  see [[base.meiva.okf.md|the base overview]].
- `run/` exists so ephemeral runtime artifacts (pidfiles, sockets, session
  records) do not litter the home's top level.
- `$MEIVA_HOME/disabled_services` records the operator's durable disabled set.
  An empty file means no services are disabled; an absent file reads the same.
  Operator starts rewrite it, while internal restarts and the watchdog read it.
- Daemon pidfiles live only under `$MEIVA_HOME/run/`.
- Operational procedures that act on these paths (backup/restore, log reading,
  recovery) are in `.agents/skills/meiva-guide/operations/`.
