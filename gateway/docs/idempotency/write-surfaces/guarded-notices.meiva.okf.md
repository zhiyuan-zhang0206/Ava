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

The positive observed ID is read from `GET /api/notices/open?agent_id=<id>&include_awaiting=true`
before an SDK `meiva.ui.edit_notice` / `dismiss_notice` call; no open notice
means nothing is sent. Browser, CLI and IM bridge resolution use the keyed
resolve path; the browser's mounted reply component retains a key for manual
same-intent recovery after failure. A changed action, reply or target gets a new
key. The resolve response is 201 with the original optional `inbound_id`;
`status` may reflect current agent state. Replay repairs only the original
pending wake and cannot resurrect completed/deleted inbound work. Existing
action/kind rules and deliberate later FYI read-with-reply operations remain
unchanged.

A caller retries an ambiguous response under the same key and observed row; the
receipt replays the original acceptance. The existing domain receipt table is
reused without FK or TTL retention. There is no client journal.
See `gateway/agents/docs/notice-receipts.meiva.okf.md` for the transaction and
event rules.

## Notice ID regression setup

`gateway/tests/events/test_notices_resolve_history_live.py` derives its local-ID
skew from the current table's `MAX(id)` to distinguish local and global IDs.
That value is not the sequence high-water mark: fixture truncation can remove
rows without resetting the sequence. The setup assertion `open_local != open_id`
is therefore essential; the offset alone does not guarantee distinct IDs across
all prior allocation histories. Consumer assertions require that premise.
