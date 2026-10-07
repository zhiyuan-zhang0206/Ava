---
type: doc
title: Chat inbound transaction ownership
description: Native caller-owned chat insertion and the existing standalone commit and wake wrapper
---

# Chat inbound transaction ownership

`insert_chat_inbound_in_transaction` accepts a connection already inside its
caller's transaction. It enforces the existing caller protocol, normalizes
caller payload/provenance, and writes the keyed chat identity and any central
audit/impersonation facts on that connection. It returns the existing
`ChatInboundReceipt` and an optional recorded telemetry event. It opens no pool
or transaction and performs no commit, live emission, wake or resurrection.
The caller may combine these facts with other domain writes, then commit or
roll back the whole operation. Emit the returned event and publish any wake
only after a successful commit. An idle connection, including autocommit outside
an explicit transaction, is refused before writing; implicit INSERT transaction
creation is insufficient for the caller-owned contract.

`insert_chat_inbound_once` remains the standalone compatibility owner. It opens
its existing transaction context, invokes the native writer, explicitly commits,
emits the recorded event and wakes only a newly inserted inbound, in that order.
Its signature, `ChatInboundReceipt` return, source semantics, conflict detection
and duplicate handling are unchanged. Gateway delivery/reconciliation and SDK
outbox consumers retain their existing post-commit and resurrection policies.

`client_message_id` still lives on the inbound row: identical body/source/target
replays recover the original id, changed immutable identity raises
`ClientMessageConflictError`, and distinct or absent keys retain their existing
meaning. This refactor adds no receipt table or permanent tombstone; deleting
that row still removes this identity's evidence. The primitive is not a provider
source admission or execution guarantee. It changes no Weixin seen-set, routing,
command uncertainty or cursor behavior.

Owners: [[base/agents/messages/docs/caller_protocol.ava.okf.md]] and
[[base/agents/messages/docs/inbound-provenance.ava.okf.md]].
