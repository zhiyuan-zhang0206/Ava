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
├── mcps/<name>/                # installed MCP packages, each with its own .venv
├── plugins/<name>/plugin.py    # externally installed plugins
├── machine_name, machine_host, machine_serve_*   # setup fields written by `ava start` flags
├── disabled_services           # durable `--disable-service` set the watchdog honors
└── deploy-state.json           # posture mirror: the offline "updating" label (R1, Task #1021)
```

`~/.ava/clusters.json` is the exception — a **host-level** registry keyed by home
path (overridable with `AVA_CLUSTER_REGISTRY`), shared across every cluster on
the box, so it lives in the default home regardless of which unit reads it.

## Notes

- **`.env` rewrites are serialized across processes — at every door.** There are
  four: `runtime_config.write_fields` (the config panel / ops `config_write`),
  `envfile.upsert_env` (converge, on **every `ava start`**), `envfile.remove_env`,
  `runtime_config.rename_env_keys`, and `cli/enroll.py:write_bootstrap_env` (a full
  replace). Each holds `shared/platform.py:file_lock` on the sibling `.env.lock`
  (`envfile.env_lock_path`) for its whole read-modify-write, with a bounded wait —
  `LockTimeoutError` on expiry rather than writing unsynchronized. Locking one door
  orders nothing: the interleave that matters is converge's `upsert_env` against the
  gateway's or the ops daemon's `write_fields`.
- The doors are **leaves and must stay leaves** — none may call another. `fcntl`
  locks are per open file description, so a nested take blocks on itself and
  surfaces only as a `LockTimeoutError` after the full bound. `snapshot_env` is
  deliberately lock-free for the same reason: it is called from inside them.
- The lock file is a SIBLING because `file_lock`'s POSIX branch opens its path with
  `"w"`, which truncates — pointed at `.env` it would empty the secrets it guards.
  In-process thread safety is separate (`services/agent_ops/daemon.py:_state_write_lock`);
  neither substitutes for the other.

- **`installed.json` rewrites are serialized the same way**, on the sibling
  `installed.json.lock` (`shared/install_registry.py:registry_lock`). Its writers are
  `ava skill install` in an agent's shell, `ava converge` on a restart, the gateway's
  skills-toggle handler, and `scripts/migrate_skill_identity.py --apply`; every one of
  them is a load-modify-save, and `save` is a full replace, so the same interleave
  drops a package's row while its directory stays on disk. Saving atomically (temp +
  rename) prevents a torn file and does nothing about a lost update.

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
  recovery) are in `.agents/skills/recover-a-cluster/SKILL.md`.
