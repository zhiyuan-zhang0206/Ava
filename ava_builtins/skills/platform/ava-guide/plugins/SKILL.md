---
name: plugins
description: "Routes Ava plugin selection, development, and updates. Use when an extension needs runtime code, hooks, or persistent state."
---

# Plugins

Identify the package kind before choosing its lifecycle:

- **Native Ava plugin:** Python contributions declared by `plugin.py`, with
  optional configuration, services, hooks, and bundled skills. Read
  [develop](develop/SKILL.md) to build an external native plugin and verify it
  at the agent restart boundary.
- **Claude Code plugin package:** `.claude-plugin/plugin.json`, optionally
  bundled agents and MCP configuration. Read
  [packages](../packages/SKILL.md) for the supported installation mechanics.
- **Built-in Ava plugin:** kernel-shipped code under `ava_builtins/plugins/`.
  A durable source change follows the kernel contribution process (L4).

For discovery, candidate review, installation, and verification, use
[packages.install](../packages/install/SKILL.md). That flow states which
package formats the installer supports; do not assume a native plugin and a
Claude Code package install through the same path.

Use [modification-layers](../modification-layers/SKILL.md) to determine change
ownership and activation. CLI help owns plugin command syntax;
`docs/conventions/extensions/plugin-spec-v2.md` owns native contribution contracts.
