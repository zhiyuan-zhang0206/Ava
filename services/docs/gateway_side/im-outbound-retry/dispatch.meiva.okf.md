---
type: doc
title: "Outbound dispatch, uncertainty and retention"
description: "One send per daemon, whole-send retry on proved no effect, uncertain outcomes recorded without retry."
tags:
- im-bridge
- idempotency
---

# Outbound dispatch, uncertainty and retention

One send runs per daemon. A short row-lock transaction changes one `queued`
intent to `sending`, storing its attempt token and `send_result_unconfirmed`
before provider access. No database lease spans the external call; a pool with
one connection suffices. A short transaction records the outcome with the
original attempt/status CAS.

Only queued intents are eligible. Restart cannot prove an old sender stopped:
unresolved sending stays unchanged, and its original attempt may still finish.
Sending and uncertain rows are never automatically resent and do not block later
queued messages. There is no cross-process chat gate, lease or TTL stealing.
Different daemons may overlap distinct intents, reorder messages or interleave
chunks; one daemon remains sequential.

Cancellation collects already-started external work (including SDK threads) or
commits. A crash after claim can lose a message even before provider access;
acknowledgement loss leaves it unconfirmed. Ambiguous transport failure after a
successful prefix is uncertain. Only `SendNotStartedError` proof of no whole-send
effect permits a whole retry: the worker retries the entire send once and
records `failed` when the retry also proves no effect. Ambiguous outcomes
record uncertain without retry. Unknown errors record uncertain then reach
the service owner unchanged; if recording fails, sending stays unconfirmed and
both errors survive. Reasons/logs never contain raw HTTP exception credentials.

All intent identities and replay receipts are retained without automatic expiry
in this slice. `sent` means the adapter completed its whole accepted manifest;
this is not an exactly-once provider guarantee. A multi-chunk uncertain attempt
may have delivered any prefix; there are no persisted chunk acknowledgements,
resumable chunk retries or automatic reconciliation yet.
