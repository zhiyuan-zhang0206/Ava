---
type: doc
title: Event contracts
description: Declared event vocabulary, schemas and live delivery.
tags: [base]
---

# Event contracts

`base/events/` owns declared event vocabulary, schemas and live delivery.
Its component nodes describe the current contracts and implementation.

`base/events/contract.py:EVENTS` merges the typed `EventSpec` declarations in
`base/events/declarations/`. Payload schemas, categories, tiers and derived SQL
key fragments come from that owner; `base/events/registry.md` is generated from
it. `base.telemetry.emit` rejects undeclared event names.

## Documented components

- [[base/events/live/docs/live/live.ava.okf.md]] — Live Event Channel (`ava:events`).
