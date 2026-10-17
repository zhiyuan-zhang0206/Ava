---
type: doc
title: Schedule runner composition
description: Current ownership boundaries and their executable evidence.
---

# Schedule runner composition

The runner composes SDK policy inputs from one ConfigBoot and binds the schedule actor to that process's clients. Its checkout anchors launched scripts; admission rejects a development checkout against a production home before reading or executing a script. The submitted revision remains the execution authority rather than whichever script happens to be current when a stale runner wakes.

```meiva-contract
id = "runner-revision-admission"
kind = "invariant"
statement = "A stale runner revision cannot execute a newer script, while current revision admission is recorded before successful completion."
code = ["services/wake/schedule_manager/runner.py:<module>"]
tests = ["gateway/schedules/tests/test_runner_admission.py::test_stale_runner_revision_cannot_execute_newer_script", "gateway/schedules/tests/test_runner_admission.py::test_current_revision_is_applied_before_clean_completion"]
```
