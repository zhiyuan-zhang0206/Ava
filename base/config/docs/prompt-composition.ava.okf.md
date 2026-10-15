---
type: doc
title: Prompt configuration
description: Behavioral constraints retained alongside their implementation and tests.
tags: [configuration]
---

# Prompt configuration

```ava-contract
id = "sdk-defaults"
kind = "invariant"
statement = "The default SDK expansion is the measured common module set; rare APIs remain discoverable through ava.help, and explicit wildcard expansion remains available. Plugin expansions are merged before these defaults. Skill selection and preloading are per-agent settings so a spawner can narrow one worker without changing the cluster catalog."
code = ["base/config/domains/agent/prompt.py:<module>"]
tests = ["agent/graph/tests/prompt/test_system_prompt.py::test_field_default_is_production_p95", "agent/graph/tests/prompt/test_system_prompt.py::test_plugin_expansions_merge_with_p95_without_duplicates"]
```
