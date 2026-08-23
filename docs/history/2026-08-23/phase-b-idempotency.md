# Phase-B idempotency timing

Phase B now grants each agent-runner update dispatch 120 seconds: the operation
can spend up to 30 seconds fetching before it validates, pauses, and starts the
platform-specific updater. This covers the measured slow-host path without
turning an ordinary update into an artificial retry.

Same-key retries of `cluster_update` now wait for the original dispatch's
stored outcome until 180 seconds after its database claim. The bound preserves
the 900-second no-progress guard's ability to expose a wedged spawn while
leaving the three-second dedup wait unchanged for lifecycle and spawn work.

A completed fetch with a nonzero exit status is logged before migration
validation fails closed, so the operator-visible diagnosis retains the actual
fetch failure.
