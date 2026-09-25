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

`cli/commands/_root_driver.py:_wait_for_service_tree` checks the selected root
roster after launch. A running process is insufficient: every service needs a
fresh identity-bound protocol verdict. Root generations are observed before and
after probing, so a replacement or stopped generation invalidates the response.
Unavailable IPC is unknown, not proof that a unit stopped. Repeated positive
stopped observations can end the wait early.

The core serving functions receive `SERVICE_READY_TIMEOUT_S`; other services
receive `NON_CRITICAL_SERVICE_READY_TIMEOUT_S`. Both kinds of incomplete result
prevent startup success. The frontend is included, even while it builds. There
is no readiness waiver for boot, update, or recovery callers.

The CLI reports 0 only for a fully ready selected roster, 4 for incomplete
readiness, and 1 for failed setup or launch. `shared.start_serving` admits work
only after that successful generation is recorded. Failed startup preserves the
closed serving boundary and the evidence needed for an idempotent retry.

Operator status uses `_probe_service`, which returns unknown for unavailable or
missing identity probes. It does not substitute a bare HTTP response, TCP connect,
or PID-file liveness. Startup failure alerts retain their durable episode identity
and resolve after a later positive readiness observation.

Parent: [[cli/commands/start-readiness/start-readiness.ava.okf.md|start readiness]].
