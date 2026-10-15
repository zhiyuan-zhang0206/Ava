---
type: doc
title: Host configuration composition
description: Current ownership boundaries and their executable evidence.
---

# Host configuration composition

One host boot provides live policy readers, clock factories, machine identity, plugin configuration authority and the process home. A profile-restricted process defers construction of the all-domain Settings view until the authority needs it. The shared graph retains the installation's registry for the process lifetime; plugin configuration reads can remain live without pretending the graph can change its registry. Logging initialization closes the supplied clients on failure while preserving the primary exception.

```ava-contract
id = "host-configuration-owner"
kind = "invariant"
statement = "The host root supplies its configuration authority and producer without eagerly constructing a complete Settings view for a profile-restricted process."
code = ["services/agent_runner/agent_host/lifecycle/configuration.py:<module>"]
tests = ["services/agent_runner/agent_host/tests/test_boot_sdk_posture.py::test_host_root_supplies_model_and_config_owners", "services/agent_runner/agent_host/tests/test_boot_sdk_posture.py::test_profiled_host_defers_complete_model_until_first_service_read"]
```
