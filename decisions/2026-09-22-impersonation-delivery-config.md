# Configure the impersonation delivery budget

The user requested configuration with a 180-second ACK window and at most two
total delivery attempts by default. Both limits are cluster configuration;
zero is rejected so the termination rule cannot accidentally become unlimited.

Each request snapshots its policy on the lease. Reading process-local settings
on every reservation was rejected: relays and native runtimes can restart or
refresh configuration at different times, and an edit must not shorten an ACK
window already promised to an executor. Existing leases keep their original
300-second window during migration; new requests use the configured policy.

The SQL counter no longer caps every lease at two. Serialized reservations
enforce the saved limit. Downgrade refuses open leases with a policy the old
code cannot honor, or any recorded count above two that the old constraint
cannot represent; it never truncates durable history to make rollback pass.
