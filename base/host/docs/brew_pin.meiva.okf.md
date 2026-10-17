---
type: doc
title: "Operator-approved tool pins"
description: "The no-lockfile tool pins in base/host/brew_pin.py: the Homebrew formula manifest, the canonical uv release with its asset digests, the apt-series pins, and the warning-only brew probes."
tags:
  - base
  - ops
---

# Operator-approved tool pins

`base/host/brew_pin.py` owns the operator-approved tool pins that have no lockfile: the
Homebrew formula manifest, the uv release (version plus the four platform asset digests) and the
apt-series pins for PgBouncer and Redis. The brew probes are warning-only drift detection for
`meiva converge`: an absent brew or a process error degrades to `None`/empty sets, a missing or
unpinned formula is reported while an uninstalled one is not, nothing mutates a pin, and no work
runs at import time. `brew_prefix` falls back to `/opt/homebrew` when brew is absent so callers
read a harmless path instead of crashing, and the `brew list` subprocess is bounded by
`_BREW_TIMEOUT_S` so a wedged brew cannot stall lifecycle commands.

UV_VERSION and UV_ASSET_SHA256 are the canonical uv pin: scripts/provision/toolchain.sh and every setup-uv workflow step embed the same version, toolchain.sh embeds the same digests, and drift fails the pin contract tests.

PGBOUNCER_APT_VERSION and REDIS_APT_VERSION are the canonical apt-series pins; scripts/provision/database.sh and the CI install steps embed the same strings and drift fails the pin tests.

The brew probes are warning-only drift detection: an absent brew or a process error degrades to None or an empty set, and the converge step stays silent when brew is absent or non-macOS while reporting each missing or unpinned formula.

Parent: [[base/host/docs/host.meiva.okf.md]].
