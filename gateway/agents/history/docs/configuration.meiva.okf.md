---
type: doc
title: History configuration owners
description: Current ownership boundaries and their executable evidence.
---

# History configuration owners

History policy construction captures an owner without reading it. Each operation reads the retained live ConfigBoot and obtains a ClockConfig through `base.clock.configuration`. Gateway-profile understanding options read the same authority's environment file rather than constructing an agent domain. A newly built Clock retains that read's values; another operation may see a later overlay.

```meiva-contract
id = "live-history-owner"
kind = "invariant"
statement = "History policies remain independent for two configuration owners, and later reads observe edits to their own owner."
code = ["gateway/agents/history/configuration.py:<module>"]
tests = ["gateway/tests/test_understanding_build_endpoints.py::test_history_policies_keep_two_real_config_owners_and_live_overlays"]
```
