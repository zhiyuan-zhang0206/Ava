---
type: doc
title: Config CLI
description: '`ava config` is the gateway config client and a settings-free local `.env` repair surface.'
tags:
- tool
- configuration
---

# Config CLI

`cli/commands/config.py` implements `ava config get/set/unset`. The normal
path is a thin client for `GET/PUT /api/config`: it resolves the gateway URL
and cluster bearer credential from the process environment or unit files, then
sends only the requested merge-patch delta. It never restarts processes; the
gateway response names the restart targets.

`--local` operates directly on this unit's `$AVA_HOME/.env` and never dials the
gateway. It reads aliases, sensitivity, scope, editability, type, choices, and
restart metadata from `shared.config_registry`; sensitive values are masked.
Before writing, it validates the full affected candidate through
`shared.config.candidate`, so a cross-field-invalid patch cannot replace the
only local config file. Host fields are locally writable; a pure runner cannot
write cluster fields locally because its cluster configuration is fetched from
the gateway.
