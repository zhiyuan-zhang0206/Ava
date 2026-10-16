---
type: doc
title: Schedule entry composition
description: Current ownership boundaries and their executable evidence.
---

# Schedule entry composition

Runner-hosted callbacks borrow their explicit ScheduleInputs without rebinding the shared SDK. A fresh standalone process owns its loaded image, ConfigBoot, one lazy database admission gate and client set. It binds an SDK context with that boot's clock factory and restores the previous context on exit. A primary callback failure survives a secondary client-close failure; normal close failure remains visible.

```ava-contract
id = "entry-admission-owner"
kind = "invariant"
statement = "A fresh schedule entry retains the loaded image and one admission gate for database handles instead of relabeling code after checkout changes."
code = ["schedules/entry.py:<module>"]
tests = ["schedules/tests/test_catchup.py::test_schedule_entry_retains_the_loaded_image_gate"]
```
