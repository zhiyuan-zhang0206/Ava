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
reader explicitly. `flat_dump()` retains the owner's validated model values;
`current_field_values()` freshly decodes the owner's file on every call. Missing
file fields keep that owner's model values, including profile-excluded domains;
invalid values fail validation. The reader neither memoizes fresh values nor
mutates the runtime. Bootstrap provider keys and plugin configuration are explicit
projection inputs, retaining their declaration and secret-delivery boundaries.

Profiled hosts and lite SDK roots use `ConfigAuthority.deferred()` with their
explicit complete-model factory. Installing a surface retains the factory without
upgrading config. The first read that needs a profile-excluded domain constructs and validates the
complete model from the current environment, preserving the existing config-service
first-use semantics. Later reads reuse a successfully constructed model. A failed
construction propagates its original exception; a subsequent explicit read may
construct again, as with the previous success-only memoization. The eager
constructor still validates its supplied complete model immediately. File values
remain fresh on every read and use the path captured by the composition root.

Refresh timing remains consumer-owned: completion policy, outbox limits and
flush cadence read fresh values where they previously did. Outbox
`DeliverySenderConfig` belongs to the Installation and caches its send-path tuple
only at that sender's first send. Independent senders do not share a snapshot.
This ownership change leaves the existing settings facade and lite boot lifecycle
in place; it does not retire all ambient configuration reads.
