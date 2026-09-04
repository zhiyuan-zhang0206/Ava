# Managed writer publication foundation

## Decision

Managed-writer publication reuses
`deployment_state.managed_writer_evidence` as a strict version-two envelope.
A committed `current` record binds the exact all-unit release tuple. A
`pending` record preserves that predecessor while freezing ordinary births
during an update. Pending collection adoption and explicit abandoned-operation
recovery both require a fresh, complete closure under the currently live
rollout lease; lease expiry alone never clears or promotes evidence.

The prepared-inventory producer has two intentionally different digests.
`inventory_digest` identifies the narrower `ExpectedUnitWriters` tuple consumed
by the unit observer. `prepared_receipt_digest` binds the whole sealed receipt,
including service-only roster declarations. Publication records retain both;
collection adoption compares the full receipt digest after the trusted producer
has checked the observer tuple. The narrower observer digest cannot alias a
receipt whose service roster changed.

Synchronous and asynchronous admission use the same SQL lock order and pure
classification contract. Stable SQL `NULL` retains the never-enabled legacy
behavior, a transition defers admission without discarding agent state or
inbound work, and current publication requires the exact loaded unit, canonical
selector, full inventory receipt, and complete registered-unit roster.
Malformed, unknown, or empty version-two evidence fails closed.

The focused proof workflow is path-scoped to the managed-writer modules and
their contract tests rather than to one development branch. This keeps the
publication proof active for later changes without making it a duplicate job
for unrelated pull requests.

## Boundary retained

This is storage, transaction, and admission-classification foundation only.
No updater producer, normal-service readback, agent admission callsite, service
activation, or protocol-one switch is connected. Bootstrap observations are
not normal ready-service evidence, and the durable restarter remains the sole
owner of process replacement and restart completion. A future activation must
connect trusted runtime producers and preserve those existing boundaries; this
change does not authorize a rollout.
