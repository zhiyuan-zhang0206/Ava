# Lifecycle application is not runtime observation

`applied_at` means the exact target incarnation's durable decision was installed
atomically. It does not mean the process died or a restart finished.
`observed_at` requires an actual successor admission for restart, or separately
verified runtime exit for termination. A pre-exit callback is not OS-exit proof.

Process claim accepts one restart/terminate before dispatch. Application writes
the target-fenced status decision and timestamp in one transaction, replacing
the graph's retrying status-only `_flip_to_restarting` writer. Applied executors
cannot claim another request. Restarts remain internal execution state, not a
new public lifecycle state.

Successor admission is allowed while the pointer remains active: otherwise
waiting for admission to observe would deadlock admission itself. After actual
admission CAS, the same transaction observes the applied restart and clears its
pointer before the successor can claim the next message. Targets never change.

The initial process integration is not ready for activation: legacy dispatch
fixtures must use real ownership; hosted apply must move to its existing
single-flight safe-settlement boundary; termination exit observation and the
external process launcher/crash path must be integrated and verified. The
self-respawn fallback remains a competing execution mechanism to remove or
reduce to the same command, not a new authority to accept another request.

No generation can revoke an already-running external side effect. The verified
old-writer shutdown barrier remains necessary, and no exactly-once guarantee
is asserted. Database-only tests of the admission function are not evidence of
an actual OS process stopping and starting; CI must cover that boundary too.
