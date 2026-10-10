---
type: doc
title: Converge host wiring
description: The firewall, redis_bridge and hook-drift warning steps and their guard rails.
tags:
- cli
---

# Converge host wiring

Cold start passes its admitted service roster into preparation before publishing
the desired service file. Service-specific steps require a selected consumer;
host wiring and private data-plane preparation remain shared dependencies.
Standalone converge resolves the persisted selection and capability gates.
Each converge operation creates one lazy `ConfigBoot` and passes it through the
required `ConvergeCtx.config` field. OS job registrations use live readers from
that owner's view, including the package refresh cadence and WAL-G schedule.
The WAL-G preparation and job steps read the same `view.walg.walg_config_file`;
clearing it still retires the existing job without an OS registration gate.
Creating the context does not deliver configuration or register an OS job.
The first selected config reader captures inputs already delivered by startup;
converge itself does not repeat dotenv delivery or replace process environment.
Collector, browser and frontend preparation skip unselected services. Native
LGTM downloads only selected backends, and invokes Loki's config validator only
when Loki is selected. Readiness still checks every selected service.

PostgreSQL preparation follows runtime binary resolution: an existing vendored
tree remains selected; otherwise a complete installed PostgreSQL 17 toolchain
and its pgvector files are validated without downloads. The installed tools and
`pg_config` must describe the same installation; pgvector must include the base
install script named by its control file's default version. Missing tools, another major,
or missing extension files fail before data-plane startup. Only an absent
installation triggers the pinned vendored distribution. Linux arm64 can use its
native PostgreSQL 17 packages without requiring an unrelated download artifact.
Remote-managed data planes skip local server and extension preparation entirely.

- `firewall.py` reconciles the per-binary Application Firewall manifest.
  Version-stamped Python, Postgres, Homebrew, browser, and observability paths mean
  an upgrade can orphan the old ALF identity while loopback keeps working — issue
  #949. The step adds and unblocks resolved manifest paths, then removes stale
  managed rules. These `socketfilterfw` mutations were empirically verified without
  elevation on the macmini running macOS 15.3.1; other versions fall back to
  `sudo -n` and then an exact manual command without blocking `ava start`.
  See
  [[base/docs/base.ava.okf.md|Base Library]].
- `_steps.ensure_local_git_hooks` warns when any conventional local
  checkout's Git hook installation is missing or drifted (via
  `provision/check_git_hooks.py --scan-machine`); warn-only, it never blocks a
  start or update. See the runbook's Git hooks section.
- `redis_bridge.py` installs the repo-owned pure-stdlib relay into
  `$AVA_HOME`, converges or retires its macOS KeepAlive job as the cluster shape
  changes, and exposes the authenticated Redis PING used by `ava status` and the
  alert-only cluster health check. The listener recreates its socket after an
  interface or descriptor failure; Redis itself never widens beyond loopback.
