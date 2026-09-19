---
type: doc
title: Package Update Policy & Channels
description: '`ava packages status` / `refresh` / `rollback` / `policy` — the content-channel surface over registry schema v2: the derived host version, per-machine channels, per-package policy, and the refresh executor (tasks #2915 / #3267).'
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
machine, from where, under what policy". A package whose manifest excludes this
host (`engines.ava` / `requires_commit`) is listed with its blocking reason.
`--json` is the machine-readable form.

## `ava packages refresh`

```bash
ava packages refresh [--check] [--package NAME] [--force] [--json] [--from-job]
```

One pass that makes this machine's channel-backed **skill** packages match
their sources (design §5.3; plugin/MCP applies land in P2):

- **core channel** (repo-native content): `git ls-remote` the checkout's remote
  → fetch commit objects when the ref moved (never the working tree — the same
  objects-only contract as `shared.cluster_drift.prod_source_fetch`) →
  `git diff --name-only <applied_rev> <head> -- <path>` picks the packages that
  changed → `git archive` into `$AVA_HOME/skills/.<name>.new` → gates → staged
  swap (marker-protected subtrees carried; the replaced tree is kept as
  `.<name>.prev` for rollback).
- **git channel** (a user install with a recorded `source`): the existing
  `acquire_source` path; a ref pinned to a tag/commit never auto-advances.
- **Gates** (any failure keeps the disk as-is and records the outcome): the
  tree must carry a `SKILL.md`; `shared/skill_scan.py` critical findings refuse
  with no `--accept-risk` on any automatic path; the manifest host contract
  (`engines.ava` vs the derived version, `requires_commit` ancestry) records
  `blocked_version`; and the local-edit guard never overwrites a hand-edited
  copy — `--force` is the human-only override.
- **Records** (`UpdateState.last_result`): `up_to_date | applied |
  available: … | blocked_version: … | conflict: … | refused_scan: … | error: …`;
  consecutive failures back the check interval off (doubled per failure, capped
  at a week, ±10% jitter).
- **Skips**: a per-home flock (no concurrent passes), a cluster update in
  flight, and for `--from-job` also the OS-jobs / refresh switches. Manual runs
  check on demand; job runs honor each package's due cadence and backoff.
- The pass **never restarts anything** and **never writes the checkout**; a
  landed skill activates at the next skill scan. One OS job per machine runs
  `ava packages refresh --from-job` (`shared/os_packages.py`, 15-minute base
  tick; per-package cadence is registry data).

## `ava packages rollback <name> [--force]`

Restores the previous tree kept at `skills/.<name>.prev` by the last apply
(swaps it with the current one; marker-protected subtrees ride along). The
local-edit guard refuses unless `--force`. The channel watermark (`applied_rev`)
is left where it was — a later refresh applies only what changed after the
revoked rev.

## `ava packages policy <name> [--update-mode auto|notify|off] [--check-every 24h]`

Records an explicit policy on the row; at least one field is required — a
bare `ava packages policy <name>` is a usage error before any command runs,
and the `--check-every` duration is validated at the parse layer. Explicit
values survive every refresh pass, while unset fields resolve from the
settings defaults at first sight
(`notify` records `available` without applying; `off` is never checked).
`ava skill install … [--update-mode …] [--check-every <dur>]` records the same
fields at install time.

## Key Dependencies

- Parent: [[packages.ava.okf.md|Package Commands]]
- [[install_registry.ava.okf.md]] — schema v2 (rows carry `update`; the registry carries `channels`)
- [[okf/skills/load-directory-sync.ava.okf.md]] — the load directory this pass writes (the fourth bulk writer)
- [host versioning](../../../conventions/host-versioning.md) — the derived host version the gates compare against
- Design: [core-package-update-channel](../../../future/infra/core-package-update-channel.md) — tasks #2915 / #3267
