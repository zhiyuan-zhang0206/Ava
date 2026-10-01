---
type: doc
title: "`$AVA_HOME` Layout"
description: '`base/paths/__init__.py` resolves every per-unit path from `$AVA_HOME`. One home = one unit; co-located units keep separate state because their homes differ. Helpers mkdir on first access, so calling one means the directory is ready.'
tags:
- base
- library
- filesystem
---

# `$AVA_HOME` Layout

## What it is

`base/paths/__init__.py` is the single-point resolver for every per-unit path. The root
comes from `base.host.env.dotenv_boot.resolve_ava_home` (env `AVA_HOME`, default
`~/.ava`), read on every call — never captured at import; each
helper `mkdir(parents=True, exist_ok=True)` on first access, so calling one
means "this directory is ready and writable".

All three process classes — gateway, agent, exec subprocess — inherit the same
`AVA_HOME` variable, so nothing is passed by hand. **The home is the unit's
identity**: a co-located gateway unit at `~/.ava_gateway` and a runner unit at
`~/.ava` share no state, because every path below derives from a different root.

## Layout

```
$AVA_HOME/
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
├── milvus-data/                # milvus-lite data dir
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
+ its stable `pty-allocation.lock`). Pointing `AVA_HOME` at a temporary directory
therefore isolates all of them. The home lists no clusters: each home describes
only itself.

## Notes

- **`.env` / `installed.json` lock discipline** (sibling file locks at every door, leaves stay leaves, atomic save vs lost update): [[base/paths/docs/lock-discipline.ava.okf.md]].
- The home is `AVA_HOME` when set, else `~/.ava`
  (`base/host/env/dotenv_boot.py:resolve_ava_home`), never from cwd and never from a
  flag: no pointer file, no checkout claim, no in-process override. A home that
  carries its own `<home>/source` checkout is operated only by that checkout's code
  (`dotenv_boot.home_checkout_error`; `prod_service_checkout_error` applies it to
  service launches). Cluster identity **is** this path — there is no cluster name;
  see [[base.ava.okf.md|the base overview]].
- `run/` exists so ephemeral runtime artifacts (pidfiles, sockets, session
  records) do not litter the home's top level.
- `$AVA_HOME/disabled_services` records the operator's durable disabled set.
  An empty file means no services are disabled; an absent file reads the same.
  Operator starts rewrite it, while internal restarts and the watchdog read it.
- Daemon pidfiles live only under `$AVA_HOME/run/`.
- Operational procedures that act on these paths (backup/restore, log reading,
  recovery) are in `.agents/skills/operating-ava-cluster/`.
