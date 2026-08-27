---
type: doc
title: "`$AVA_HOME` Layout"
description: '`shared/paths.py` resolves every per-unit path from `$AVA_HOME`. One home = one unit; co-located units keep separate state because their homes differ. Helpers mkdir on first access, so calling one means the directory is ready.'
tags:
- shared
- library
- filesystem
---

# `$AVA_HOME` Layout

## What it is

`shared/paths.py` is the single-point resolver for every per-unit path. The root
comes from `settings.general.ava_home` (env `AVA_HOME`, default `~/.ava`); each
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
├── .ava_home                   # (in a dev *checkout*, not here) pointer to that worktree's home
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
├── skill_match_cache/          # derived skill vectors, keyed by live-catalog fingerprint
├── mcps/<name>/                # installed MCP packages, each with its own .venv
├── plugins/<name>/plugin.py    # externally installed plugins
├── machine_name, machine_host, machine_serve_*   # setup fields written by `ava start` flags
├── disabled_services           # durable `--disable-service` set the watchdog honors
└── deploy-state.json           # generation-guarded cluster UI maintenance owner
```

`~/.ava/clusters.json` is the exception — a **host-level** registry keyed by home
path (overridable with `AVA_CLUSTER_REGISTRY`), shared across every cluster on
the box, so it lives in the default home regardless of which unit reads it.

## Notes

- **`.env` / `installed.json` lock discipline** (sibling file locks at every door, leaves stay leaves, atomic save vs lost update): [[shared/paths/lock-discipline.ava.okf.md]].
- The home is resolved **checkout-anchored** by
  `shared/dotenv_boot.py:resolve_ava_home`, never from cwd and never from a
  flag: `AVA_HOME` env > the prod source checkout → `~/.ava` > the checkout's
  `.ava_home` pointer > `~/.ava` flagged *unanchored* (which plants an
  unreachable DB sentinel so a never-`ava start`ed dev checkout fails loud
  instead of writing to prod). An `AVA_HOME` that **contradicts** the checkout's
  own claim raises `AvaHomeContradictionError` instead of resolving; the callers
  for which that mixing is deliberate (the install, `ava cluster down/destroy`,
  the test suite) set `AVA_HOME_OVERRIDE=1`. Cluster identity **is** this path —
  there is no cluster name; see [[shared.ava.okf.md|the shared overview]].
- `run/` exists so ephemeral runtime artifacts (pidfiles, sockets, session
  records) do not litter the home's top level.
- A marker file's **name** is part of the contract with the operator, so renaming
  one needs a migration or the recorded intent goes unread — silently, since an
  absent marker is a legal state. `disabled_services` was `skipped_services`
  before the affirmative-naming refactor; the converge step *legacy
  disabled-services marker*
  (`shared/disabled_services.py:migrate_legacy_marker`) carries a pre-rename file
  over on every `ava start` / `ava update` / `ava converge` and logs what it
  moved plus the resulting disabled set. When both names exist the current name
  stays authoritative (it is what the code writes, so it is the operator's later
  word — an empty file included) and the legacy one is kept as
  `skipped_services.superseded`: evidence rather than silence when the two
  disagree. The one other renamed path, `$AVA_HOME/<service>.pid` →
  `run/<service>.pid`, was handled with a permanent dual read
  (`legacy_pid_path()`), which is why this one is a one-shot rename instead.
- Operational procedures that act on these paths (backup/restore, log reading,
  recovery) are in `.agents/skills/operating-ava-cluster/`.
