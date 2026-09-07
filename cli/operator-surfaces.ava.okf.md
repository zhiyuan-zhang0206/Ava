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

- [[cli/config.ava.okf.md|`ava config get/set/unset`]]
- `ava pty freeze/status/resume`: host-wide PTY allocation gate.
- `ava logs rotate`: top-level copytruncate rotation for service stdout and
  native backend logs at 64 MiB or a UTC-day boundary.
- `ava logs retention`: local, non-recursive managed-log cleanup; legacy global
  14-day fallback or explicit family tiers across service and native archives;
  open handles are excluded.
- `ava pitr retention inspect`: read-only latest local dry-run plan; no delete
  surface.
- `ava mcp ...`: isolated environments at `$AVA_HOME/mcps/`. `ava mcp serve`
  runs the other direction and exposes the cluster control plane as an MCP
  server ([[ava/mcps.ava.okf.md|MCP]]).
- `ava plugins ...`
- `ava skill install/update/upgrade/enable/disable/register/scan/trust`
- `ava presets ls/get/create/update/delete`
- `ava schedules ls/get/create/update/delete/provision/start/stop/restart/logs/runs`:
  provision creates the built-in schedules from `schedules/manifest.json` and
  also runs at gateway boot.
- `ava trace ship`
- `ava lgtm on/off/status`: observability-stack toggle on this host, represented
  by its marker and native lifecycle.
