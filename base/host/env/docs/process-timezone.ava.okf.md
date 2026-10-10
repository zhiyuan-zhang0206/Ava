---
type: doc
title: Process timezone delivery
description: Explicit startup timezone application and the read-only configuration attachment boundary.
tags:
- configuration
- environment
---

# Process timezone delivery

`base/host/env/dotenv_boot.py:apply_process_timezone` owns the process clock
operation. Its caller supplies the resolved authoritative timezone, or `None`
when no authority exists. The operation does not read Settings, `AVA_TIMEZONE`
or a timezone field default. A valid IANA name sets `TZ` for child processes and
calls `time.tzset` on platforms that support it. Missing authority leaves the
host clock unchanged. An invalid name that bypassed configuration validation
also leaves the clock unchanged, preserving the existing startup behavior;
raw configuration input still fails at its validation boundary.

`ConfigBoot.prepare` applies this operation after its startup environment and
configuration fields have been prepared. `ConfigBoot.read_process_environment`
captures an existing delivery without applying it, including during later eager
model construction. SDK attachment therefore never changes the process TZ.
The legacy `base.config.apply_cluster_timezone` facade resolves its existing
cluster authority and calls the same operation; it does not own a second
implementation.

The operation is defined in the existing environment delivery owner, below
configuration composition. ConfigBoot imports that definition directly, so a
standalone configuration owner does not import a private operation from the
root Settings facade. The exact entry module and its static `__all__` declare
this operation alongside the owner's existing environment delivery interfaces.
This change does not retire the legacy Settings facade or its full-build graph.
