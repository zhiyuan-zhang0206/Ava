---
type: doc
title: Caller protocol admission
description: Generation-bound chat source admission and the remaining rollout barrier.
tags:
- shared
- identity
---

# Caller protocol admission

`require_caller_protocol` locks the target agent's current runtime generation,
owner, kind and fresh lease in the same transaction as the chat inbound INSERT.
Protocol version must be at least 1 and status must be running or idling.
Unknown, expired, terminated and legacy targets fail before INSERT with an
actionable error; there is no source downgrade or installed-commit shortcut.
The check applies to direct chat persistence as well as HTTP delivery.

MCP `send_message` accepts opt-in `caller_protocol='v1'`. The format declaration
grants no permissions: a verified, non-revoked write-scope client is still
required. The server derives external MCP provenance from its client row ID;
body source/instance assertions are rejected, and delivery uses this same gate.
Omitted protocol retains legacy attribution, an explicitly unresolved default
until coordinated producer activation. The opt-in is not that activation.

CLI opt-in profiles pass through authentication and this gate unchanged. The
persisted `caller_identity` sidecar is asserted provenance, never authority.
The actual hosted claim/envelope path reads it without representing an external
tool as the human or system. Legacy source traffic keeps its existing path.

Actual process and hosted admission can advertise the compiled reader's protocol
only after the same transaction validates activated current publication against
the loaded image/selector/full receipt and admits the complete resource set.
Legacy remains protocol 0; pending defers, unknown resources refuse. Ordinary
same-owner hosted settlement preserves its advertisement. Column presence, an
environment flag and a deployed source tree are not barrier proof. No deployment
or production activation is performed by this code change.

New/dead targets cannot receive a v1 bootstrap prompt: lifecycle ingress remains
blanket-fenced until a reviewed durable intent -> actual consumer admission ->
locked prompt INSERT handoff exists. Direct lifecycle writes are not loosened.

Regression proof covers real CLI/profile, authenticated gateway, locked INSERT,
actual hosted claim and envelope; unknown/expired/legacy/terminated rejection;
and an ownership replacement blocked until the INSERT transaction finishes.
