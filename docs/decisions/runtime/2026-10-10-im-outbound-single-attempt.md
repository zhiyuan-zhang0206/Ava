# IM outbound uses one automatic attempt per intent

## Context

The outbound worker held a transaction-scoped chat advisory lock across the
provider call and borrowed a second database connection to claim and finish.
This pinned a PostgreSQL backend during network waits and required a pool of at
least two connections. The lock also justified converting old sending rows to
uncertain, but strong chat serialization makes crash recovery a blocking concern.

## Decision

Use existing durable intents and attempt tokens: atomically claim queued to
sending in a short transaction, release the database lease, perform the accepted
manifest once, and record an outcome with the original attempt/status CAS.
Never automatically replay a claimed or ambiguous intent. Leave unresolved
sending unchanged and visibly unconfirmed; a still-live original owner retains
completion authority. Later queued intents may proceed independently.

Keep one sequential send owner per daemon and collect actual SDK work before
cancellation returns. Unexpected errors remain failures: record uncertainty and
then propagate the original exception to the service owner.

## Alternatives rejected

A strict cross-process chat gate preserves ordering but holds a backend during
network waits. A durable sending barrier avoids that transaction but can block
an entire chat after a crash indefinitely. Lease expiry or claim stealing cannot
prove that an earlier provider call stopped and can duplicate delivery. None of
these stronger guarantees is required for this personal-agent delivery policy.

## Consequences

A crash after claim but before send can lose a message. Success without a
committed acknowledgement remains unconfirmed. Different daemons can overlap
or reorder distinct intents and interleave chunks; one daemon remains sequential.
A partially delivered manifest is never resent wholesale. This is one automatic
attempt per immutable intent, not exactly-once provider delivery. Explicit replay
still creates a separately authorized immutable intent. There is no schema,
lease, expiry or reconciliation framework change.
