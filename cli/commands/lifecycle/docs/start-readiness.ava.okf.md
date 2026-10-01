---
type: doc
title: Start readiness
description: One selected root roster, exact process ownership, and fresh protocol readiness gate ordinary start.
tags:
- cli
- lifecycle
- readiness
---

# Start readiness

`ava start` first validates the persisted home (initialized by `ava init`) and service selection. The
pre-bind gate in `cli/commands/lifecycle/start.py:_refuse_occupied_health_ports` refuses
ports held by another unit before launching application processes. It reads the
same selected roster that the root will own. A matching HTTP response from an
unrelated process is insufficient evidence of ownership.

`cli/commands/lifecycle/root_driver.py` owns admission, launch and observation of the
single application root. An idempotent start observes the live root generation;
a cold start launches one through the platform's native custody boundary.
macOS places the permission helper above the root. Linux starts the root under
the caller or systemd without a helper.

An eligible macOS GUI handover returns a `StartDelegation` from the locked start
body. The lifecycle wrapper leaves its lock and maintenance authorization before
executing that handover. The GUI child runs ordinary locked start and owns
readiness and resume. The observer cannot release the maintenance hold or start
services in its own non-GUI domain when delegation fails.

After launch, every selected service, including the frontend, must have fresh
identity-bound protocol evidence. Root generation changes invalidate the result.
Startup returns success and publishes serving only when the complete roster is
ready. Every caller receives the same verdict; no readiness waiver
turns an incomplete launch into success. Failed launches retain their diagnostic
record through `base/deploy/lifecycle/launch_failures.py`.

## Related contracts

- [[cli/commands/lifecycle/docs/readiness-verdict.ava.okf.md]] — exit codes,
  generation evidence, deadlines and alerts.
- [[cli/docs/start_identity.ava.okf.md]] — init identity and start admission.
- [[base/cluster/docs/machine.ava.okf.md]] — capabilities and the selected service roster.
