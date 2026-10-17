---
type: doc
title: "HTTP Operation Identity and Retry Safety"
description: "Business receipts, principal-scoped keys, verified retry policies and write-surface inventory."
tags:
- gateway
- idempotency
---

# HTTP operation identity and retry safety

`base/api_contracts/contracts.py` owns route retry semantics. A write verb,
final-state equality, an RPC response cache or a single-flight lock alone does
not prove that repeating a request is safe. Cancel can stop later work; restart
can interrupt the process started by the first call. Until a domain protects
these effects, its route declares `NON_IDEMPOTENT`.

`base/api_contracts/idempotency.py` owns strict supplied-key validation (string,
1–128 characters); omission is handled by each caller boundary, never by
replacing an invalid key. SDK creation and MCP principal keys share this rule.

## Identity and acceptance

One operation ID identifies one intent. A deliberate second identical operation
gets a new ID. Retries preserve the original ID and immutable request. Do not
identify intent by content hashes, clock buckets or caller labels. Content
hashes may compare requests after their identity has been established.

The handler commits its domain receipt with its business rows or outbox event
in one database transaction. A same-ID, same-request duplicate returns the
original receipt ID, including under concurrent requests and after a lost
response. A changed request returns 409 without a second effect. The domain
owns immutable fields (including defaults, target and execution policy).
Receipt success means accepted, not delivered or executed. Commit before
returning acceptance; a later wake failure must not erase the receipt.

`gateway.http.auth.request_principal.request_key` owns HTTP key scoping. Use the
logical operation method and concrete path on delivery and reconciliation.
Every request that carries an `Idempotency-Key` also carries
`Idempotency-Scope: principal-v1`; a key without it, an unknown scope value, or
`principal-v1` without a bound principal fails with 422 before any durable
write. Supplied keys contain 1 to 128 characters; malformed keys fail the same
way. A route whose key is optional (chat messages and schedule
mutations) keeps its one-shot behavior when the key is absent and gives no
ambiguous-retry promise; a required-key route rejects a missing key. New
handlers must distinguish a missing key from an explicitly empty one.

`principal-v1` binds verified principal, method, path and caller ID into the
stored identity (`base.api_contracts.idempotency.stored_key`); there is no raw
caller-key namespace. MCP credentials scope to the authenticated client ID. Chat
messages are the one cluster-wide identity: `chat_message_key` fixes the stored
`client_message_id` to the cluster administrator so delivery and reconciliation
land on the same receipt. See [[gateway/http/auth/docs/request_principal.meiva.okf.md]]. Do not try
another namespace after a conflict.

Chat `inbound_messages` rows already own acceptance and pending processing;
do not put another queue in front of them. No HTTP route uses a generic response
cache: every `AT_LEAST_ONCE_WITH_KEY` handler owns its key in the same
transaction as its business effect. `api_idempotency` holds agent-ops dispatch
receipts only.

## Retry consumers

The SDK POST and PATCH wrappers inherit route semantics. A keyed route always
sends one `Idempotency-Key` and `Idempotency-Scope: principal-v1` pair
(`keyed_headers`), shared by every retry of the call, and retries ambiguous
outcomes (read timeout, lost response, 429/502/503/504): the handler owns the
key in its business transaction, so a repeat returns the original receipt.
PATCH and DELETE otherwise permit ambiguous retries only for routes declaring
natural `IDEMPOTENT` semantics. Unknown routes fail closed. Unprotected writes
may retry ConnectError, ConnectTimeout and PoolTimeout (before sending), but
never ReadTimeout, WriteError or HTTP 5xx. An explicit SDK POST override remains
a caller assertion; callers must not use it to bypass unprotected gateway
writes.

The browser fetch wrapper makes one HTTP attempt. Its chat receipt recovery
uses the original message ID; it must not extend that retry loop to unprotected
writes. CLI management uses one-shot HTTP dials. CLI/SDK chat delivery retries the HTTP call under one
key and has no client outbox; this program does not introduce client/browser
outboxes. MCP chat uses the same domain inbound transaction.
Agent-ops RPC keys cover their existing dispatch boundary only: HTTP command
retries are not protected by generating a new RPC key for each attempt.
External IM sends need provider-specific receipt and uncertainty handling;
HTTP acceptance does not prove a provider effect occurred exactly once.

## Server outbox recovery requirements

Business mutation and immutable outbox event commit together. Claim workers
with durable lease ownership and fence completion writes against a stale owner.
Retry with bounded backoff and preserve identity through all downstream hops.
A lease alone cannot fence an external effect: the recipient must deduplicate,
or the adapter must preserve an inspectable uncertain outcome. Persist success,
terminal failure or uncertainty; exhausting a budget cannot silently succeed
or delete pending records. Make pending and failed work inspectable.

Retain receipts while delivery is pending and for the supported retry horizon.
Never expire pending identities into fresh operations. A domain that prunes
receipts must define an explicit expired-ID rejection/recovery policy before
claiming retry safety. Domain workers own crash-window and concurrency tests.

## Write-surface inventory

See [[write-surfaces/write-surfaces.meiva.okf.md]] for the audited write boundaries.
