---
type: doc
title: Explicit Configuration Authority
description: 'Process-owned boot values and fresh configuration reads.'
tags:
- configuration
---

# Explicit Configuration Authority

Composition roots construct `base.config.service_read.ConfigAuthority` with their
runtime model, a complete `Settings(profile=None)` read model and an absolute
unit `.env` path. The SDK Installation and application/service roots carry this
reader explicitly. `flat_dump()` retains the owner's validated boot values;
`current_field_values()` freshly decodes the owner's file on every call. Missing
file fields keep that owner's boot values, including profile-excluded domains;
invalid values fail validation. The reader neither memoizes fresh values nor
mutates the runtime. Bootstrap provider keys and plugin configuration are explicit
projection inputs, retaining their declaration and secret-delivery boundaries.

Refresh timing remains consumer-owned: completion policy, outbox limits and
flush cadence read fresh values where they previously did. Outbox
`DeliverySenderConfig` belongs to the Installation and caches its send-path tuple
only at that sender's first send. Independent senders do not share a snapshot.
This ownership change leaves the existing settings facade and lite boot lifecycle
in place; it does not retire all ambient configuration reads.
