---
type: doc
title: Immutable upload storage and native identity
description: Strict manifests, protocol proof and physical-root receiving quota.
tags: [base, uploads]
---

# Immutable upload storage and native identity

`models` owns ordered immutable manifests, source acceptance, mutable delivery
status and independently versioned native request/proof. Protocol 1 is required
as an actual integer; omission, bool, float and unknown values fail before effects.
Unit identity reuses `base.cluster.authority.unit.UnitIdentity`; native machine and
resolved `AVA_HOME` identify the receiver, not the physical uploads quota owner.

`paths` owns the shared upload URL, path, image MIME, limits and safe-serving
helpers used by gateway, Ops and inbound image consumers. `agent_upload_dir`
lives under `Path.home()/Downloads`, independent of `AVA_HOME`.
`storage` uses the shared agent xact gate and counts flat files, legal nested final
objects and receiving reservations for actual machine/resolved directory. Source
and copy manifests sharing a physical root count each batch path once. Existing
unbound silent reservations are retained conservatively. Primitive dot-temps are
held staging, excluded from final inventory and never swept by this owner.

Hidden `.delivered-v1/{batch}/{UUID-ordinal-suffix}` objects publish through native
`create_private_bytes`: no overwrite, only matching size/hash recovery, then fsync.
Verified manifest paths cannot escape native root. Suffix/type/name validation
precedes reservation; basenames reserve 38 bytes for the native temporary name.
Original display filenames are not physical paths.

`source` owns retained acceptance and single-inbound outcome with no TTL/cleanup FK.
Accepted outcome is consulted before mutable target facts; HOLD cannot accept a
late proof. New placement and inbound acceptance share locks and transaction.
See the [Gateway domain contract](../../../../gateway/upload_delivery/docs/delivered-uploads/delivered-uploads.ava.okf.md)
for source rounds, remote Ops proof, authentication and legacy compatibility.
