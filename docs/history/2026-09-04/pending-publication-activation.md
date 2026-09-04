# Pending publication activation reconstruction

PR #1562 was reconstructed on top of the merged publication foundation and the
authoritative runtime-publication-input rebuild. Only its service-activation
contract was retained; the historical runtime preparation, bootstrap, observer,
and publication-foundation commits were not replayed.

The updater needs authority that survives a detached unit update without
turning a typed readback into a bearer credential. The chosen boundary keeps
the operation, observation challenge, migration receipt, selector transition,
and exact service readbacks in the existing pending publication journal. Each
effect rechecks the live rollout lease in a short transaction, while the
updater keeps the existing host-local serialization across the following OS
operation. Database locks never span process or network work.

The complete prepared receipt and the observer writer tuple remain distinct.
Selector version two and activation planning bind `prepared_receipt_digest`;
`inventory_digest` continues to identify only `ExpectedUnitWriters`. This
prevents a changed normal-service roster from aliasing an unchanged observer
tuple.

Publication requires the exact ordered all-unit readback set and atomically
replaces pending with current. Exact retries return the original publication
identifier, while conflicting observations refuse instead of overwriting the
journal. The infrastructure `ava-agent-host` service is allowed, but numeric
agent and attempt sessions remain outside this service-only authority. Runtime
admission and updater callsite integration remain separate changes.
