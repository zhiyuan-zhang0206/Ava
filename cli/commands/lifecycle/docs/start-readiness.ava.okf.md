---
type: doc
title: Start readiness
description: Selected core protocol readiness gates start; optional capabilities remain diagnostic.
tags:
- cli
- lifecycle
- readiness
---

# Start readiness

`ava start` first validates the persisted home (initialized by `ava init`) and service selection. The
pre-bind gate in `cli/commands/lifecycle/start.py:_refuse_occupied_health_ports` refuses
ports held by another unit before launching application processes. It reads the
selected core roster; optional port conflicts cannot block the core.

`cli/commands/lifecycle/root_driver.py` owns admission, launch and observation of the
single application root. An idempotent start observes the live root generation;
a cold start launches one through the platform's ordinary process owner.
macOS places the permission helper above the root. Linux starts the root under
the caller or systemd without a helper.

The CLI's existing caller-owned child list retains a directly launched root
through readiness, any later command work and the CLI telemetry drain, just as
it retains directly launched PostgreSQL children. A failed readiness attempt
does not release that handle. An embedded caller keeps the same explicit list
for as long as it needs the native PID retained; root launch neither reaps nor
terminates a child when a start phase returns.

An eligible macOS GUI handover returns a `StartDelegation` from the locked start
body. The lifecycle wrapper leaves its lock and maintenance authorization before
executing that handover. The GUI child runs ordinary locked start and owns
readiness and resume. The observer cannot release the maintenance hold or start
services in its own non-GUI domain when delegation fails.

After launch, selected core services must be running and answer their protocol.
Optional services are reported without blocking serving or adding a startup wait.
Protocol availability is an observation, not a proof that an endpoint belongs to
a particular native process generation. Process ownership for actual signals
remains with the launch/stop owner. The rollout launch-failure record contains
only core launch failures.

## Related contracts

- [[cli/commands/lifecycle/docs/readiness-verdict.ava.okf.md]] — exit codes,
  core deadlines, optional availability and alerts.
- [[cli/docs/start_identity.ava.okf.md]] — init identity and start admission.
- [[base/cluster/docs/machine.ava.okf.md]] — capabilities and the selected service roster.
