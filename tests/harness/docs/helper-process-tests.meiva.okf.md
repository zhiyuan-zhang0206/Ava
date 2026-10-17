---
type: doc
title: Helper process contract tests
description: Socket-free spawn doubles, durable ownership and real ancestor-chain probes.
tags: [tests, process, helper]
---

# Helper process contract tests

### Helper spawn double

The test double accepts variable arguments to forward the production call unchanged
to its scenario callback. Its existing `reportIncompatibleMethodOverride` exception
is confined to that test override; the real client signature remains authoritative.

### Helper record and identity

Durable session records own spawn idempotency. Final graceful delivery accepts the same birth-time tolerance as resolution and rejects a birth outside that tolerance.

A helper's volatile nursery table cannot replace the persistent record. The
whole-second drift case preserves the macOS boot-time correction rationale:
resolution and final signal delivery must agree about the accepted birth.

### Helper ancestor chain

The former direct-parent assumption mistook `unit -> root -> helper` for an
orphan and terminated the unit during boot. The tests preserve that geometry.

### Helper probe executable

The `S603` exception belongs only to this fixed test executable. It does not
admit operator-supplied executable text, and the call retains its 60-second bound.
