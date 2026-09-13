---
type: doc
title: Package Update Policy & Channels
description: '`ava packages status` — the read-only surface over registry schema v2: the derived host version, per-machine channels, and per-package update policy/state (tasks #2915 / #3267).'
tags:
- cli
- packaging
---

# Package Update Policy & Channels

## `ava packages status`

```bash
ava packages status [--json]
```

`status` joins the install registry (schema v2: per-package `update` policy +
per-machine `channels`) with the derived host version and each package's
declared manifest range: kind, origin, channel, mode/interval, applied rev,
last result, next check — the one place to look for "which content is on this
machine, from where, under what policy". `--json` is the machine-readable
form.

## Key Dependencies

- Parent: [[packages.ava.okf.md|Package Commands]]
- [[install_registry.ava.okf.md]] — schema v2 (rows carry `update`; the registry carries `channels`)
- [host versioning](../../../conventions/host-versioning.md) — the derived version the manifest gates compare against
- Task #3267 — the executor verbs (`refresh`, `rollback`, `policy`) land with the content channel (P1) and extend this node
