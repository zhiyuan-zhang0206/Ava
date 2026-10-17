---
type: doc
title: Config CLI
description: '`meiva config` is the gateway config client and a settings-free local `.env` repair surface.'
tags:
- tool
- configuration
---

# Config CLI

`cli/commands/management/config.py` implements `meiva config get/set/unset`. The normal
path is a thin client for `GET/PUT /api/config`: it resolves the gateway URL
and a bearer (a delivered machine API token, the gateway's human secret, or a
remote unit's capability token) from the process environment or unit files, then
sends only the requested merge-patch delta. It never restarts processes; the
gateway response names the restart targets.

`--local` operates directly on this unit's `$MEIVA_HOME/.env` and never dials the
gateway. It reads aliases, sensitivity, scope, editability, type, choices, and
restart metadata from `base.config.config_registry`; sensitive values are masked.
Before writing, it validates the full affected candidate through
`base.config.admin.candidate`, so a cross-field-invalid patch cannot replace the
only local config file. Host fields are locally writable; a pure runner cannot
write cluster fields locally because its cluster configuration is fetched from
the gateway.

Keys that decide who authenticates to the cluster are read-only on every config write path (the API, the ops op and
`--local`): `MEIVA_CLUSTER_SECRET` rotates only through
`scripts/data_plane_ops/rotate_cluster_secret.py`, and
`MEIVA_ALERTS_WEBHOOK_TOKEN` / `MEIVA_MCP_ENDPOINT_ENABLED` (it opens `/mcp`, where
generation-independent MCP client tokens authenticate) change only by editing
the gateway `.env` on its host. Otherwise any authenticated caller, a machine token included, could pick
a credential that outlives its own admission. Writable secrets are outbound
credentials (provider, search, chat and backup-store keys) only;
`base/config/tests/test_config_editing.py` pins that classification.
