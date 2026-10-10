---
type: doc
title: Install Registry Update Policy Inputs
description: 'Schema-v2 channels and the explicit package defaults reader used by resolved_policy.'
tags:
- base
- library
- packaging
---

# Install Registry Update Policy Inputs

## Schema v2 — update policy & channels

`Registry.version` is 2 since the content-channel support. Beside the per-row
`update` described in the parent registry node, the registry carries a top-level `channels` map
(`ChannelState`: one row per channel kind — remote URL, ref, last seen head,
last check). The reader accepts exactly v2: a v1 file is refused (the
lazy-migration shim is retired — every writer has been v2-only since #2355),
and a file carrying a *newer* version is refused as before — no build guesses
at another shape; `load()` itself never writes.
The surface over all of it: `ava packages status`
([[../../../../../cli/commands/extensions/packages/docs/packages.ava.okf.md|the package commands]]).

## Explicit defaults

`resolved_policy()` requires a `defaults_reader` supplied by the owning entry.
Only a missing mode or interval on a channel-backed row invokes that reader,
at the original short-circuit read point. A row with no channel or fully
explicit policy does not read defaults. The reader's validated values and
failures propagate; the registry neither captures configuration nor selects a
global owner.
