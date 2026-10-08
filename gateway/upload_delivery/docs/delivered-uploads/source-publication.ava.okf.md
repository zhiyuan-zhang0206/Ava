---
type: doc
title: Delivered upload source publication and quota
description: Original physical identity, immutable file publication and shared native quota admission.
tags: [gateway, uploads]
---

# Delivered upload source publication and quota

`base.agents.upload_delivery.source` owns `upload_delivery_batches`. Tx A takes
the existing agent upload xact gate, consults retained key identity **before**
mutable target existence, then freezes source/target `UnitIdentity`, manifest,
physical root and verified `InboundProvenance`. The target is the unique serving
runner unit whose URL matches the machine's registered Ops address. Receiver
compares actual machine/home; a changed registry URL cannot silently redirect a
batch to another home. Unbound or ambiguous fresh placement rejects before claim.

Source receiving files are published outside a borrowed DB connection with
`create_private_bytes`, create-only verification and directory fsync. Tx B commits
original 202 acceptance and pending delivery intent together. Receiving identity
and reservation never expire; interrupted source publication needs original key
and bytes. Ready replay precedes mutable target/deletion checks. No cleanup of
finals, unbound staging or other attempts is performed.

Physical finals use `~/Downloads/AvaAgent-{id}/.delivered-v1/{batch}/{object}`.
`agent_upload_dir` is independent of `AVA_HOME`: two homes on one machine may
share this root. `storage.check_quota` serializes through the existing agent xact
gate and counts actual flat files, legal hidden finals and receiving reservations
bound to machine/resolved directory. Same batch paths count once across source
and receiver homes. Old silent receiving rows have no physical binding and are
conservatively retained in accounting, never assigned or deleted by inference.
Native primitive dot-temp files are staging; hard-crash leftovers are held and
excluded beside manifest reservation, without erasing them. Legacy flat unknown
files count normally. Legacy source and upgraded remote dispatch admission share this quota owner.
Remote HTTP finishes before borrowing its pool; the short file-write transaction
charges only net bytes/files when replacing an existing flat path.

New legacy writers reject the reserved directory basename. Even an old remote
writer sanitizes separators into flat names and cannot overwrite nested finals.
Legacy writers have no durable reservation: a disconnected late flat writer can
still exceed quota; this existing boundary is not claimed fixed.

The authenticated manifest-bound GET `.../uploads/{batch}/objects/{ordinal}`
serves only ready source-owned objects, after hash/size verification. It accepts
no arbitrary filesystem path, survives historical agent-row deletion, sends
attachment/nosniff headers and is separate from old native-image upload URLs.
