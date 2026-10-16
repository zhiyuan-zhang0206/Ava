---
type: doc
title: Schedule router contracts
description: The gateway schedules router's write-side behavior — script checks, projections, log reads, the catalog-dependent draft — and its SQL RETURNING invariant.
tags: [gateway]
---

# Schedule router contracts

The HTTP router syntax-checks scripts before writing them. Its list projection
omits script bodies; full reads retain them. Newly enabled rows are picked up by
reconciliation without a separate launch call. Log reads remove trailing blank
PTY screen padding before selecting the requested content tail. The saved PTY
transcript preserves output after a session is reaped; if neither live capture nor
transcript is available, the stored crash error remains a distinct fallback.
Only the agent-backed natural-language draft operation requires a model catalog;
ordinary schedule management keeps its existing independent requirements.

```ava-contract
id = "sql-returning-contract"
kind = "invariant"
statement = "Schedule values remain bound SQL parameters; interpolated projection columns are the module-owned fixed column list. Inserts and updates use RETURNING after creating or locking their target, so non-null row assertions express database postconditions rather than validating user input. Full reads keep a separate missing-row 404."
code = ["gateway/schedules/router.py:update_blocking", "gateway/schedules/router.py:create_blocking", "gateway/schedules/router.py:fetch_full_blocking", "gateway/schedules/router.py:control_blocking"]
tests = ["gateway/tests/extensions/test_schedules_api.py::TestCreate::test_create_returns_full_view_and_initial_version", "gateway/tests/extensions/test_schedules_api.py::TestReadUpdateDelete::test_get_missing_404", "gateway/tests/extensions/test_schedules_api.py::TestReadUpdateDelete::test_put_description_only_writes_no_new_version", "gateway/tests/extensions/test_schedules_api.py::TestReadUpdateDelete::test_put_enabled_change_routes_through_immediate_session_sync"]
```

## Relationship to Other Nodes

- [[gateway/schedules/docs/schedules.ava.okf.md|Schedule Manager & Runner]] — the overview these route contracts belong to.
