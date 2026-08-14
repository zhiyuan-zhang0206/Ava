---
type: doc
title: Task Maintenance
description: Overview index of the gateway-side task reminder service registered by the ava_fleet plugin. Contains 1 sub-concept.
---

# Task Maintenance

## What it is

A gateway-side daemon registered by the `ava_fleet` plugin into the ops service roster — overdue task reminders + escalation. The domain belongs entirely to fleet, so it lives under the plugin namespace (`plugins/ava_fleet/services.py` declares `ServiceSpec`, `ops/spec.py:_plugin_services()` discovers and folds it into the single-source `build_services()`).

## Sub-concepts

- [[task-maintenance.ava.okf.md|Task Maintenance]]
