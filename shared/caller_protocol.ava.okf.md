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

CLI opt-in profiles pass through authentication and this gate unchanged. The
persisted `caller_identity` sidecar is asserted provenance, never authority.
The actual hosted claim/envelope path reads it without representing an external
tool as the human or system. Legacy source traffic keeps its existing path.

All production admission helpers still advertise protocol 0. Activation requires
independent proof that old unconditional writers/consumers have been stopped or
upgraded. Column presence and a deployed source tree are not that proof. CI
models a completed barrier only in an isolated fixture, after actual hosted
admission; there is no production activation flag or endpoint in this slice.

New/dead targets cannot receive a v1 bootstrap prompt: lifecycle ingress remains
blanket-fenced until a reviewed durable intent -> actual consumer admission ->
locked prompt INSERT handoff exists. Direct lifecycle writes are not loosened.

Regression proof covers real CLI/profile, authenticated gateway, locked INSERT,
actual hosted claim and envelope; unknown/expired/legacy/terminated rejection;
and an ownership replacement blocked until the INSERT transaction finishes.
