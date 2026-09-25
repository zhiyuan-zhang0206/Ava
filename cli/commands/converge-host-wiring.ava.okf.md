---
type: doc
title: Converge host wiring
description: The _converge_firewall, _converge_redis_bridge and hook-drift warning steps and their guard rails.
tags:
- cli
---

# Converge host wiring

Cold start passes its admitted service roster into preparation before publishing
the desired service file. Service-specific steps require a selected consumer;
host wiring and private data-plane preparation remain shared dependencies.
Standalone converge resolves the persisted selection and capability gates.
Collector, browser and frontend preparation skip unselected services. Native
LGTM downloads only selected backends, and invokes Loki's config validator only
when Loki is selected. Readiness still checks every selected service.

- `_converge_firewall` reconciles the per-binary Application Firewall manifest.
  Version-stamped Python, Postgres, Homebrew, browser, and observability paths mean
  an upgrade can orphan the old ALF identity while loopback keeps working — issue
  #949. The step adds and unblocks resolved manifest paths, then removes stale
  managed rules. These `socketfilterfw` mutations were empirically verified without
  elevation on the macmini running macOS 15.3.1; other versions fall back to
  `sudo -n` and then an exact manual command without blocking `ava start`.
  `_gateway_ready` uses the same audit when an off-box probe fails. See
  [[shared/shared.ava.okf.md|Shared Libraries]].
- `_converge_steps.ensure_local_git_hooks` warns when any conventional local
  checkout's Git hook installation is missing or drifted (via
  `provision/check_git_hooks.py --scan-machine`); warn-only, it never blocks a
  start or update. See the runbook's Git hooks section.
- `_converge_redis_bridge` installs the repo-owned pure-stdlib relay into
  `$AVA_HOME`, converges or retires its macOS KeepAlive job as the cluster shape
  changes, and exposes the authenticated Redis PING used by `ava status` and the
  alert-only cluster health check. The listener recreates its socket after an
  interface or descriptor failure; Redis itself never widens beyond loopback.
