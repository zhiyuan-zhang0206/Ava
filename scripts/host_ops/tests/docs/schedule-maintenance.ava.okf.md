---
type: doc
title: Installed-runtime schedule maintenance proof
description: Repeating the external schedule writer contract against a selected installed checkout.
tags: [testing, schedules, operations]
---

# Installed-runtime schedule maintenance proof

`test_update_schedule_script.py::test_external_file_uses_installed_runtime_and_real_writer`
copies the maintenance script outside the checkout and runs it with the selected
installation's real interpreter and writer. The temporary home and database
remain test-owned. The assertions preserve enabled/stopped state and the pause
journal, and check the stored script bytes without executing the script body.

The default source is `Path(sys.prefix).parent`, the installation running pytest.
Set `AVA_TEST_INSTALLED_SOURCE` to an isolated older checkout with its own real
`.venv` to repeat this test against that runtime. A default run proves the current
installation only; it does not establish compatibility with an older revision.

```ava-contract
id = "installed-runtime-writer"
kind = "invariant"
statement = "The external maintenance script uses the selected installation's interpreter and real writer while preserving schedule enabled/stopped state and the pause journal; it stores the new body without executing it."
code = ["scripts/host_ops/tests/test_update_schedule_script.py:test_external_file_uses_installed_runtime_and_real_writer"]
tests = ["scripts/host_ops/tests/test_update_schedule_script.py::test_external_file_uses_installed_runtime_and_real_writer"]
```

Parent: [[scripts/docs/scripts.ava.okf.md]].
