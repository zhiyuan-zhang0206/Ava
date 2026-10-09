---
type: doc
title: Guarded Notice Write Surfaces
description: Versioned explicit-row resolution and observed-row edit/withdrawal receipts.
tags: [gateway, notices, idempotency]
---

# Guarded Notice Write Surfaces

| Route | Policy | Effect / receipt |
| --- | --- | --- |
| `POST /api/keyed/v1/agents/{agent_id}/notices/{notice_id}/resolve` | keyed | explicit global row resolution and original optional reply inbound share one transaction |
| `PATCH /api/agents/{agent_id}/notices/current/guarded-v1` | keyed | observed global row edit and immutable `NoticeItem` acceptance in one transaction |
| `POST /api/agents/{agent_id}/notices/current/dismiss/guarded-v1` | keyed | observed global row withdrawal; stale A cannot withdraw newer B |

All require a valid caller key, explicit principal-v1 scope and verified
principal on every request. The positive observed ID is the global feed ID,
not the SDK local notification ID. Receipt replay precedes mutable target and
expiration checks, survives deletion, and never selects the later current row.
A receipt is historical acceptance, not proof the row is still active.

The edit/withdrawal paths remain server-only. Browser resolution uses the fixed
keyed path; its mounted reply component retains a key for manual same-intent
recovery after failure. A changed action, reply or target gets a new key.
The resolve response is 201 with the original optional `inbound_id`; `status`
may reflect current agent state. Replay repairs only the original pending wake
and cannot resurrect completed/deleted inbound work. Existing action/kind rules
and deliberate later FYI read-with-reply operations remain unchanged.

These paths have `legacy_keyed_retry=False`; SDK and UI current
selectors remain conservative single attempts. Unsupported routing rejects
these paths before effects. Never fall back to a legacy selector after an
ambiguous response. The existing domain receipt table is reused without new
migrations, FK or TTL retention. There is no client journal or automatic
ambiguous retry. Old CLI/IM callers remain separate. See
`gateway/agents/docs/notice-receipts.ava.okf.md` for the transaction and event rules.
