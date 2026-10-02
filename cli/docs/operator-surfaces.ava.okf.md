---
type: doc
title: CLI Operator Surfaces
description: Agent lifecycle, memory context, local operations, and package-management command groups exposed by the Ava CLI.
tags:
- gateway
- tool
- memory
---

# CLI Operator Surfaces

## Agent and context commands

- `ava impersonate request/relay`: Codex delivery requires a control endpoint
  for the existing host and uses Steer semantics. Missing endpoints fail before
  a CLI request acquires control; delivery failures stop the relay without a
  Pending-mode queue fallback. Unacknowledged messages remain for normal handoff
  ([[base/agents/impersonation/docs/impersonation.ava.okf.md|impersonation]]).

- `ava agents ls/send/cancel/restart/resurrect/terminate/kill`: `ls` renders the
  authenticated agent summary projection as stable `id / status / machine /
  label` columns. It does not expose runner-local workspace paths.
- `ava memory init`: explicitly provisions the memory-pool checkout and
  plugin-owned templates. Branch validation runs here, never during converge or
  start.
- `ava memory refresh`: triggers the gateway index refresh. Pool consolidation
  runs on the gateway schedule through `schedules/memory-steward-schedule.py`
  and `ava_builtins/plugins/ava_memory/skills/scripts/{steward,arbiter_merge}.py`.
- `ava memory search QUERY [--limit K] [--json]`: posts an authenticated search
  to the gateway. The human table preserves relative `path` values alongside
  tags and descriptions; JSON emits the gateway response's `results` list
  without converting paths or normalizing null and empty metadata.

## Local and package operations

- [[cli/docs/config.ava.okf.md|`ava config get/set/unset`]]
- `ava pty freeze/status/resume`: host-wide PTY allocation gate.
- `ava logs rotate`: top-level copytruncate rotation for service stdout and
  native backend logs at 64 MiB or a UTC-day boundary; zero-byte files are not
  rotated.
- `ava logs retention`: local, non-recursive managed-log cleanup; legacy global
  14-day fallback or explicit family tiers across service and native archives;
  open handles are excluded.
- `ava backup operations status/retire`: custody of the scheduled logical dump
  and restore-drill operations. `status` lists blocked kinds and quarantine
  (exit 1 while any kind is blocked); `retire` re-proves group closure of each
  blocked operation, and `--confirm` quarantines the proven ones
  ([[services/backup_scheduler/docs/operation-custody.ava.okf.md|Operation custody]]).
- `ava backup walg check|run|drill|restore|status`: the WAL-G physical backup. `check` is the
  pre-flight: it proves the pinned binary, the configuration and key, and a
  put/list/get/delete round trip under the bucket prefix (exit 1 on the first failing
  step). `run` is the daily tick the OS job runs: backup, verify the archived WAL chain,
  apply guarded retention (exit 1 only when a step failed; a skip or a concurrent run
  exits 0). `drill` runs the weekly recovery drill now (exit 0 only if it passed).
  `restore --dir DIR [--backup NAME] [--user U] [--time T | --lsn L]` recovers a backup into an
  empty directory with a scratch Postgres and never touches the home's data directory.
  `status` prints the configuration, key fingerprint, archiver facts and the
  last tick and drill and never fails
  ([[services/gateway_side/walg/docs/walg.ava.okf.md|WAL-G]],
  [[services/gateway_side/walg/docs/walg-restore.ava.okf.md|restore and drill]]).
- `ava mcp ...`: isolated environments at `$AVA_HOME/mcps/`. `ava mcp serve`
  runs the other direction and exposes the cluster control plane as an MCP
  server ([[ava/mcps/docs/mcps.ava.okf.md|MCP]]).
- `ava plugins ...`
- `ava skill install/update/upgrade/enable/disable/register/scan/trust`
- `ava presets ls/get/create/update/delete`
- `ava schedules ls/get/create/update/delete/provision/start/stop/restart/logs/runs`:
  provision creates the built-in schedules from `schedules/manifest.json` and
  also runs at gateway boot.
- `ava trace ship`
- `ava lgtm on/off/status`: observability-stack toggle on this host, represented
  by its marker and native lifecycle.
