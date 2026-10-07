---
type: doc
title: "HTTP Operation Identity and Retry Safety"
description: "Business receipts, legacy scope compatibility, verified retry policies and write-surface inventory."
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

`gateway.auth.request_principal.request_key` owns HTTP key scoping. Use the
logical operation method and concrete path on delivery and reconciliation.
`Idempotency-Key` is optional for legacy routes: absence retains existing
one-shot behavior and gives no ambiguous-retry promise. Supplied keys contain
1 to 128 characters; malformed keys fail with 422 before durable writes.
New handlers must distinguish a missing key from an explicitly empty one.
Unknown `Idempotency-Scope` and unverified `principal-v1` fail with 422.

Legacy REST keys retain their existing raw namespace across mixed versions.
`principal-v1` binds verified principal, method, path and caller ID;
`principal-v1:` is a reserved storage prefix. MCP credentials always scope to
the authenticated client ID. See [[gateway/auth/docs/request_principal.ava.okf.md]].
Do not silently change namespaces or try another namespace after a conflict.
Enable a client scope only after positive server capability negotiation; a
legacy server can ignore an unknown header. This change does not introduce
new capability negotiation or activate new client scopes.

Chat `inbound_messages` rows already own acceptance and pending processing;
do not put another queue in front of them. `api_idempotency` is a legacy
response cache with an execution/store crash window, not a business receipt.
New `AT_LEAST_ONCE_WITH_KEY` declarations require transactional ownership;
contract tests enforce this boundary.

## Retry consumers

The SDK POST wrapper inherits route semantics and sends one key across retries
of protected keyed calls. `legacy_keyed_retry` is enabled only for chat,
which already had keyed receipts on the rollout baseline. Newly protected
routes send a key but retain one-shot ambiguous-failure handling until positive
server negotiation is implemented. Neither a local new route declaration nor
an old server ignoring a new header proves the live server supports a receipt.
Do not set this baseline flag to activate a newly protected route. PATCH and DELETE permit ambiguous retries only for
routes declaring natural `IDEMPOTENT` semantics; keyed support for another
verb must also supply a stable key before enabling retries. Unknown routes
fail closed. Unprotected writes may retry ConnectError, ConnectTimeout and
PoolTimeout (before sending), but never ReadTimeout, WriteError or HTTP 5xx.
An explicit SDK POST override remains a caller assertion; callers must not
use it to bypass unprotected gateway writes.

The browser fetch wrapper makes one HTTP attempt. Its chat receipt recovery
uses the original message ID; it must not extend that retry loop to unprotected
writes. CLI management uses one-shot HTTP dials. CLI/SDK chat delivery has an
existing local delivery outbox; this program does not introduce or expand
client/browser outboxes. MCP chat uses the same domain inbound transaction.
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
claiming retry safety; the generic response-cache seven-day TTL is not that
policy. Domain workers own crash-window and concurrency tests.

## Write-surface inventory

See [[write-surfaces.ava.okf.md]] for the audited write boundaries.
