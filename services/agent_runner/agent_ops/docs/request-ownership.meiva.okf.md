---
type: doc
title: Ops request ownership
description: Request receipts, worker accounting and serialized configuration writes.
tags: [services, lifecycle]
---

# Ops request ownership

```meiva-contract
id = "request-ownership"
kind = "invariant"
statement = "A committed request claim remains after effect failure, cancellation or result-write failure: pending means uncertain and never authorizes re-execution. Duplicate keys validate immutable request identity and wait only for a bounded interval. Only OperationalError is eligible for connection retry. Running workers remain in maintenance accounting after their HTTP waiter is cancelled; repeated stop signals cannot interrupt bounded draining, and default-executor joining cannot prevent process exit. A modeled failed operation remains an HTTP 200 response so callers distinguish its result from transport failure; unexpected result values are string-serialized rather than breaking the control-plane response."
code = ["services/agent_runner/agent_ops/daemon.py:<module>"]
tests = ["services/agent_runner/agent_ops/tests/daemon/test_idempotent_connection_retry.py::test_dispatch_idempotent_propagates_non_operational_error", "services/agent_runner/agent_ops/tests/daemon/test_ops_route_non_json_values_degrade_to.py::test_ops_route_failed_status_is_still_http_200", "services/agent_runner/agent_ops/tests/daemon/test_ops_route_non_json_values_degrade_to.py::test_ops_route_non_json_values_degrade_to_str", "services/agent_runner/agent_ops/tests/test_request_identity.py::test_effect_then_exception_keeps_uncertain_claim", "services/agent_runner/agent_ops/tests/test_request_identity.py::test_owner_cancellation_never_frees_its_key", "services/agent_runner/agent_ops/tests/test_request_identity.py::test_result_write_failure_keeps_claim_without_reexecuting", "services/agent_runner/agent_ops/tests/daemon/test_daemon_stop_wedge.py::test_a_stop_stays_bounded_while_the_watcher_and_blocked_worker_run"]
```

```meiva-contract
id = "serialized-config-writes"
kind = "invariant"
statement = "Config and inventory read-modify-write operations share a thread lock so simultaneous workers cannot overwrite one another's fields or secrets. An event-loop lock cannot serialize these worker threads. This lock is process-local; the runtime config owner retains cross-process file locking. Blocking operations use the daemon-owned executor."
code = ["services/agent_runner/agent_ops/dispatch_sync.py:<module>"]
tests = ["services/agent_runner/agent_ops/tests/daemon/test_an_unrelated_op_still_dispatches_while_a.py::test_two_config_writes_cannot_interleave", "services/agent_runner/agent_ops/tests/daemon/test_an_unrelated_op_still_dispatches_while_a.py::test_op_arms_do_not_run_on_the_default_executor"]
```
