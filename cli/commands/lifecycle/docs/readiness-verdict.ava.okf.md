---
type: doc
title: Start readiness verdict
description: Fresh process ownership and protocol evidence gate the complete selected root roster.
tags:
- cli
- start
- readiness
---

# Start readiness verdict

`cli/commands/lifecycle/root_driver.py:wait_for_service_tree` observes the service
table and calls the selected services' protocol probes. A running process alone
is insufficient; the probe must respond. There is no native listener census or
before/after process-generation proof around read-only health checks.

Only selected core services in `CRITICAL_SERVICE_SESSIONS` gate serving. They
share `SERVICE_READY_TIMEOUT_S`. Optional services are sampled when the core is
ready; their failures are reported and emitted as `service_start_unready`, without
another waiting budget or a global startup failure. Optional launch failures also
do not enter the rollout's fatal launch-failure record.

The CLI reports 0 when the selected core is ready, 4 for incomplete core readiness,
and 1 for failed setup. `base.deploy.lifecycle.start_serving` publishes serving
after core readiness. Unknown or unavailable core protocol responses cannot
become success. Optional services may remain unavailable and recover separately.

Parent: [[cli/commands/lifecycle/docs/start-readiness.ava.okf.md|start readiness]].
