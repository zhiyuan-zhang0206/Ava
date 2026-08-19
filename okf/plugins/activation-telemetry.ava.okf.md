---
type: doc
title: Plugin Activation Telemetry
description: The runtime half of the plugin attribution ledger — one `plugin_activation` event per hook, wrap, or prompt-section firing, carrying the model in force, so philosophy §6's "removable as a gauge" is answerable with data.
tags:
- plugins
- telemetry
---

# Plugin Activation Telemetry

## What It Records
The ledger records what was registered; `shared/plugin_activation.py` records
what fired. Three surfaces emit one `plugin_activation` event per firing, keyed
by the ledger's own `(plugin, surface, identifier)` triple plus the model in
force: a **hook** that returns a non-empty state update (naming the keys it
wrote — plugin `state` writes travel through hook returns, so that surface needs
no separate probe), a **wrap** whose wrapper calls `inner` anything other than
exactly once (a short-circuit or a retry — a transparent pass-through always
runs once installed, so counting it would measure the installation), and a
**system prompt section** that renders non-empty text at build time (spawn /
compact only). `sdkNamespaces` is deliberately excluded: `agent/sdk_metering.py`
already meters it as `sdk_call`.

Framework registrations record nothing — they happen outside a `PluginContext`,
the same gate the ledger applies. Recording is a pure side channel: failures are
swallowed and never perturb hook or wrap semantics.

Two consumers read the stream. `shared/metrics_aggregate.py`'s
`plugin_activation` section counts activations by contribution and by
plugin × model — a contribution that registers but never fires is the removal
evidence [`philosophy.md` §6](../../conventions/philosophy.md) asks for. The
weekly self-evolution collector puts the same counts on each run record as
`plugins_activated`, so `mine.py` can cluster bad runs by the contribution that
acted in them.
