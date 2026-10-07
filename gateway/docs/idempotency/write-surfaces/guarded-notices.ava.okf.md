---
type: doc
title: Guarded Notice Write Surfaces
description: Versioned observed-row edit and withdrawal receipts without client activation.
tags: [gateway, notices, idempotency]
---

# Guarded Notice Write Surfaces

| Route | Policy | Effect / receipt |
| --- | --- | --- |
| `PATCH /api/agents/{agent_id}/notices/current/guarded-v1` | keyed | observed global row edit and immutable `NoticeItem` acceptance in one transaction |
| `POST /api/agents/{agent_id}/notices/current/dismiss/guarded-v1` | keyed | observed global row withdrawal; stale A cannot withdraw newer B |

Both require a valid caller key, explicit principal-v1 scope and verified
principal on every request. The positive observed ID is the global feed ID,
not the SDK local notification ID. Receipt replay precedes mutable target and
expiration checks, survives deletion, and never selects the later current row.
A 200 receipt is historical acceptance, not proof the row is still active.

These are server-only paths with `legacy_keyed_retry=False`; SDK and UI current
selectors remain conservative single attempts. Unsupported routing rejects
these paths before effects. Never fall back to a legacy selector after an
ambiguous response. The existing domain receipt table is reused without new
migrations, FK or TTL retention. See
`gateway/agents/docs/notice-receipts.ava.okf.md` for the transaction and event rules.
