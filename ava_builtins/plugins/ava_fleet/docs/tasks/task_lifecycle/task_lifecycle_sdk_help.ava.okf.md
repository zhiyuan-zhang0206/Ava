---
type: doc
title: Task Lifecycle — Generated SDK Help
description: "The create_and_assign SDK docstring source and its typed documentation contract"
tags:
- fleet
- tasks
- api
- lifecycle
---

# Task Lifecycle — Generated SDK Help

```ava-contract
id = "api-tasks-create-and-assign"
kind = "api"
statement = "Create an agent and its assigned task under one operation key.\n\nReuse the key and inputs to recover the original result after a lost reply.\nThe result proves acceptance; query task and agent state to observe\ncurrent progress. Borrowed leases are unsupported.\n\nArguments have the same meaning as in create() and ava.agents.spawn();\nmachine defaults to your own machine. The parent must exist and be open before\neither object is born.\n\nReturns:\n    A TaskAssignment; it unpacks as `task, agent_id`."
code = ["ava_builtins/plugins/ava_fleet/task_registry.py:create_and_assign"]
tests = ["ava_builtins/plugins/ava_fleet/tests/task_registry/test_update_non_owner_notifies_owner.py::test_create_and_assign_signature", "gateway/tests/lifecycle/test_sdk_task_assignment.py::test_real_sdk_response_loss_keeps_exact_body_path_key_and_pair", "gateway/tests/lifecycle/test_sdk_task_assignment.py::test_missing_invalid_key_and_retired_flag_fail_before_http", "gateway/tests/lifecycle/test_sdk_task_assignment.py::test_assignment_sends_only_explicit_config"]
api = "ava.tasks.create_and_assign"
```
