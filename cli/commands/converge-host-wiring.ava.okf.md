---
type: doc
title: Converge host wiring
description: The _converge_firewall and _converge_redis_bridge steps and their guard rails.
tags:
- cli
---

# Converge host wiring

- `_converge_firewall` reconciles the per-binary Application Firewall manifest.
  Version-stamped Python, Postgres, Homebrew, browser, and observability paths mean
  an upgrade can orphan the old ALF identity while loopback keeps working — issue
  #949. The step adds and unblocks resolved manifest paths, then removes stale
  managed rules. These `socketfilterfw` mutations were empirically verified without
  elevation on the macmini running macOS 15.3.1; other versions fall back to
  `sudo -n` and then an exact manual command without blocking `ava start`.
  `_gateway_ready` uses the same audit when an off-box probe fails. See
  [[shared/shared.ava.okf.md|Shared Libraries]].
- `_converge_redis_bridge` installs the repo-owned pure-stdlib relay into
  `$AVA_HOME`, converges or retires its macOS KeepAlive job as the cluster shape
  changes, and exposes the authenticated Redis PING used by `ava status` and the
  alert-only cluster health check. The listener recreates its socket after an
  interface or descriptor failure; Redis itself never widens beyond loopback.
