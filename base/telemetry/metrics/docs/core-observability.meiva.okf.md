---
type: doc
title: Core observability queries
description: Event-stream measurement scope and compaction effectiveness queries.
tags: [contracts]
---

# Core observability queries

## Query measurement scope

The pack is admitted by the core metric catalog under plugin `core`, using the same query validation as plugin metrics. LogQL selectors match indexed event names and agent identities; JSON decoding supplies flattened `attributes_*` fields. Selector regexes are full-string matches, unlike substring line filters. The execution envelope measures transfer activity, not an outcome; counting it as an unknown failure would dominate the outcome graph. Multi-series targets need explicit legend names because aggregate queries carry no identifying labels. Historical production row counts are not current contracts.

## Compaction effectiveness denominator

The size-ratio query collapses per-event label fan-out with `avg`, avoiding sparse single-point series. Completed counts separately show proactive, reminded, hard-with-reminder and hard-without-reminder paths. The >=95% target still requires runtime evidence; static query checks establish its expression and scope, not achieved behavior or the quality of saved files.
