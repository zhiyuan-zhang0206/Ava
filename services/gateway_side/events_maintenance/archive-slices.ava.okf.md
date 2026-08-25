---
type: doc
title: Events Maintenance — flag-gated PG events-archive slices
description: The optional PG events-archive maintenance — monthly partition roll, category-aware retention, table retention, and index-bloat governance; gated by AVA_EVENTS_MAINTENANCE_ENABLED, off by default since the LGTM cutover.
tags: []
---

# Events Maintenance — flag-gated PG events-archive slices

- **Archive slices (flag-gated)** — for clusters that still maintain the PG archive: monthly partition roll (`services/events_maintenance/partitions.py:ensure_month_partitions` — current+next month, drains months stranded in DEFAULT; idempotent carve), category-aware retention (`services/events_maintenance/retention.py:apply_retention` — drops a month partition once every category outlived its retention, prunes expired categories from live ones; audit 365d / telemetry 90d / log 30d via `AVA_EVENTS_RETENTION_{AUDIT,TELEMETRY,LOG}_DAYS`), table retention, index-bloat governance (REINDEX CONCURRENTLY on its own direct autocommit connection). Each step runs on a separate pool connection so partition DDL commits before the potentially long rollup.

Parent: [[services/gateway_side/events_maintenance/events_maintenance.ava.okf.md|events maintenance]].
