# Separate takeover authority from relay delivery

The user authorized revising the lifecycle after a valid takeover ended when
its relay lost the database while the Codex executor remained alive. A delivery
child is not the identity being borrowed: authorized fleet downtime must pause
and recover delivery without making native and external writers overlap.

We retain the original bounded TTL and explicit end boundary. Confirmed executor
death also ends authority; unknown evidence is visible and cannot extend TTL.
We rejected treating failed delivery or an exhausted message budget as identity
death. Attempt reservations remain monotonic, and late receipt remains possible.

Recovering transport requires confirmed retirement followed by one generation
claim. A new child remains blocked on the credential pipe until its birth receipt
is durable. Unknown legacy custody is withheld, never adopted by PID guess. This
upgrade cannot promise automatic recovery for generation-zero active leases
missing birth receipts; those require explicit operator ending before a new lease.

The existing machine-host TaskGroup-owned activity owns ended-lease notification independently of
relay death and native restoration. A recorded immutable destination avoids
redirecting historical notices to a replacement lease. Bounded RPCs and capped
retry backoff retain pending delivery through outages. Acceptance means host
acceptance, not executor receipt; timeout after acceptance remains at least once.
No new daemon, completion queue, permission surface, automatic renewal or
automatic retakeover was introduced.
