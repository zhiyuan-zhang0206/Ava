---
type: doc
title: Plugin Enable Config
description: '`shared/plugins_config.py` — the per-machine-local `plugins_config.json` that decides which discovered plugins load. Read-first from the local file; the cluster-wide DB singleton was dropped in decentralized-install step 3.'
tags:
- shared
- library
- plugins
---

# Plugin Enable Config

## What it is

`shared/plugins_config.py` owns which plugins are enabled on **this machine**.
Built-in plugins (`<repo>/ava_builtins/plugins/`) and external ones
(`$AVA_HOME/plugins/`) are the same concept at different filesystem locations —
the `location` field in a listing marks which — and both are gated by the same
config.

State lives in `$AVA_HOME/plugins_config.json`, scoped to the plugins actually
present on that machine. Reads come from the local file only; an absent or
empty file means "every discovered plugin enabled" (in memory — the file is
written only by the enable/disable writers). A *malformed* file is a hard
error, not a fallback: the same rule `install_registry.load` applies to
`installed.json` — a corrupt config is a state-loss signal, and silently
degrading to all-enabled would also rewrite it from nothing. The former
cluster-wide DB singleton `plugins_config_overrides` was dropped in
decentralized-install **step 3**, so enable state is now fully per-machine.

## Schema

```json
{
  "plugins": {
    "ava_compact":    { "enabled": true },
    "ava_syntax_fix": { "enabled": true },
    "audit":          { "enabled": false }
  }
}
```

## Resolution behavior

| Situation | Result |
|---|---|
| config empty | every discovered plugin treated as enabled (in memory, not written back) |
| config names a plugin not on disk | strict loaders raise `DanglingPlugin`; runtime loaders (`load_for_runtime` — plugin loading, the provider factory, gateway views) warn and drop the entry |
| same name in both the built-in and external dir | fail-fast `DuplicatePlugin` at discovery — a flat name map cannot pick a winner |
| a discovered plugin missing from the config | merged in memory as `enabled=true` (not written back) |
| malformed JSON / schema-invalid file | fail-fast — never a silent all-enabled fallback |

A plugin disabled here is imported by **no** production path: host boot and
graph build both read the enable set through `load_for_runtime`
(issue #2161 — the boot loader used to ignore it).

The failures are deliberate fail-fast: a config referring to something that
isn't there is a real drift, a duplicate name is an ambiguity, and a corrupt
file is state loss — none of them gets papered over with a default.

## Notes

- The in-process snapshot is taken **at process start**, so editing the config
  does not affect running agents — changes take effect on the next spawn or
  restart.
- Writers: `set_local_enabled` (behind `ava plugins enable|disable <name>`) and
  the gateway Control page's Plugins section (`GET/PUT /api/inventory`), which
  fans the toggle across the cluster's machines.

## Key Dependencies

- [[plugins.ava.okf.md]] — the plugin system this config gates
- [[install_registry.ava.okf.md]] — the sibling registry gating *installed* packages
