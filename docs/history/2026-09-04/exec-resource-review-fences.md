# Exec resource review fences

Independent review found three gaps between the managed exec proof and the
existing runtime contracts. The allocation had been registered before native
identity was available, so force could freeze an unattached reservation that no
terminal receipt was allowed to discharge. Hosted admission also required a
lifecycle predecessor even after a normal host process restart, leaving a known
empty set with no valid successor. Finally, the managed poll loop accumulated
output but did not preserve the existing incremental output and keepalive path.

Registration and native attachment now share one metadata transaction after the
gated owner publishes its exact identities. If force wins first, the transaction
refuses without leaving an allocation, and the original host waits for a validated
`host_eof` receipt before forgetting its local scope. Keeping a separately
dischargeable unattached reservation was rejected because it would require a new
terminal proof state and would make force distinguish "never permitted" from an
ambiguous registration commit.

A hosted successor may transfer an empty, unfrozen resource set without a
lifecycle command only when the stored same-machine host PID and birth are proven
ended under the locked admission transaction. Lease expiry or a different owner
alone remains insufficient, and unresolved or frozen sets still fail closed. The
managed exec loop now drains pending stream increments and emits silent keepalives
on the same cadence as the legacy path, with only the unpublished tail flushed at
completion.
