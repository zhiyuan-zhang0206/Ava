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
malformed file degrades to defaults (logged) rather than stranding boot. The
former cluster-wide DB singleton `plugins_config_overrides` was dropped in
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
| config empty | startup writes defaults — every discovered plugin enabled |
| config names a plugin not on disk | fail-fast `DanglingPlugin` at startup |
| same name in both the built-in and external dir | fail-fast `DuplicatePlugin` at startup |
| upgrade adds a new built-in not in the old config | auto-merged `enabled=true`, written back |

The failures are deliberate fail-fast: a config referring to something that
isn't there is a real drift, not a condition to paper over with a default.

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
