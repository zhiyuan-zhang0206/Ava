---
type: doc
title: MCP Installation & Startup Form
description: Native vs installed MCP servers (mirroring skills), the relative-path startup form, per-layer server cwd, and why not uv run.
tags:
- mcps
---

# MCP Installation & Startup Form

## Startup Form: relative paths, never uv run
For own server layers, `.mcp.json` uses a **relative** interpreter path `.venv/bin/python`. Spawn cwd comes from `ava/_mcp_config.py:server_cwd(name)` by **effective layer**: installed → its package directory (own isolated venv); built-in → **repo root** (repo venv, `-m <pkg>` finds the top-level package); plugin/machine → None (third-party command line, not reinterpreted).
**Why not `uv run`**: it is a persistent wrapper process; at the 100-300 agent density target every agent×server would hang a wrapper — pure overhead. Pinning cwd for built-in also removes the old dependency on the agent's incidental cwd.

## Installation Mechanism (native vs installed, mirroring skills)
- **native**: `<repo>/ava_builtins/mcps/*/.mcp.json`, directory-scan discovered, unregistered.
- **installed**: `ava mcp install <source>` (git URL or local path, `--path` selects a subdirectory) places a self-contained package into `$AVA_HOME/mcps/<name>/`, runs `uv sync` once to create an isolated `.venv`, registers `type="mcp"`. The daemon spawns with `cwd=installed_mcp_dir(name)` so the relative `.venv/bin/python -m <module>` resolves to the package's own venv → dependencies isolated from core. Managed via `ava mcp uninstall/upgrade/ls`.
Parent: [[okf/mcps/mcps.ava.okf.md|MCP integration]].
