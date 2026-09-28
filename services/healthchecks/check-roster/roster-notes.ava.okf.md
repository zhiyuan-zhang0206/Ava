---
type: doc
title: Readiness roster ownership
description: Service probes and diagnostic helpers are documented from their actual registrations.
tags:
- services
---

# Readiness roster ownership

`ServiceSpec.identity_probe` is mandatory for selected root units. The roster
centrally wraps network probes with native root identity, including plugin
services. `healthcheck_module` identifies protocol code for documentation;
placing a module in this directory does not schedule it.

The documentation linter compares module files with the table and with the
union of service metadata and actual imports in root diagnostic adapters. It
parses adapter source without constructing a live host roster or probing services.

PgBouncer diagnostics traverse its admin console and required listeners rather
than an end-to-end Postgres query. Redis diagnostics traverse runtime ACL PING.
Both first establish separate native data-plane custody; neither starts a
process or repairs credentials. Their resources must remain available when the
application tree is stopped for external maintenance.
