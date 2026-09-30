---
type: doc
title: Plugin Load Fail-Closed Boundaries
description: Which plugin-load failures are contained (skip + loud report) and which stay hard errors — duplicate names, malformed config, schema drift, provider registration-contract violations — plus the release probe that turns containment back into rejection.
tags:
- plugins
---

# Plugin Load Fail-Closed Boundaries

## Semantics boundary: what stays fail-closed
Containment covers *code* that fails to load. These stay hard, because
continuing would guess at the operator's intent:

- `DuplicatePlugin` — one plugin name in both the built-in and external roots.
- A malformed `plugins_config.json` (state loss, never a silent all-enabled
  fallback).
- Plugin-config schema drift at `bind_from_disk()` (points at
  `ava plugins update`).
- A provider registration-contract violation
  (`provider_api.ProviderRegistrationError` — duplicate/nested prefix,
  mismatched model, malformed price data): the flat prefix and model-id maps
  cannot pick a winner between two claimants.
- The post-load cross-model registry revalidation in the provider loader.

Unloadable `metrics.py`, `inspector.py`, `setup.py`, `plugin.py`, `services.py`
or `default_config.py` code is contained loudly (skipped and reported) at
runtime; no pre-flight rejects a plugin before it ships.
