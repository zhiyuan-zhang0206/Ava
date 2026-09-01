# Candidate validation at config write boundaries

## Decision

Every official config write boundary validates the complete affected Settings
domain before it persists a patch. The candidate starts from the fresh `.env`
file state, retains file values even when their current combination is already
invalid, applies the requested writes or default-valued removals, and runs the
same Pydantic model validators that process startup uses.

The candidate capture is bound to the exact `.env` digest supplied to the
writer's existing compare-and-swap guard. A concurrent write therefore causes a
retry rather than letting two independently valid patches persist an invalid
combined state. Candidate construction reads only the affected Settings domains,
so invalid data in an unrelated process profile does not block the write.

This decision follows the P0 incident in which an incomplete OSS transition
made every new process fail during Settings construction. Per-field validation
was rejected because it cannot prove cross-field invariants such as the
independent viewer credential required for OSS restore proof. Deferring the
check to the next process start was rejected because that failure occurs before
the process can produce its normal result envelope.

## Consequences

- Cluster API writes, host config operations, and PITR activation refuse an
  invalid candidate without changing `.env`.
- Error messages name environment-variable aliases and exclude secret values.
- Existing invalid file state remains visible to a same-domain patch until that
  patch repairs the violated invariant.
- A concurrent write is rejected before persistence and must be retried against
  a fresh candidate.
