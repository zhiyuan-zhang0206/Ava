---
type: doc
title: Per-agent configuration slices
description: Behavioral constraints retained alongside their implementation and tests.
tags: [configuration]
---

# Per-agent configuration slices

```ava-contract
id = "turn-snapshot"
kind = "invariant"
statement = "Resolved slices remain fixed for one turn; lists become tuples. Unpinned unsliced reads use the explicit owner live defaults, while pins win without leaking to another agent. ModelOverrides.from_pins reads only supplied pins, so the gateway need not own unrelated config domains. Plugin instances are cached within their own configuration view, not globally."
code = ["base/host/env/agent_slices.py:<module>"]
tests = ["base/packages/plugins/tests/test_plugin_config_view.py::TestOverrideResolution::test_built_instance_is_memoized", "base/packages/plugins/tests/test_plugin_config_view.py::TestIsolation::test_two_agents_read_their_own_config", "base/host/env/tests/test_agent_slices.py::test_two_owner_defaults_keep_resolved_slices_frozen_and_unsliced_reads_live", "base/host/env/tests/test_agent_slices.py::test_list_settings_are_frozen_into_tuples", "base/host/env/tests/test_agent_slices.py::test_overrides_from_pins_reads_only_the_pins"]
```
