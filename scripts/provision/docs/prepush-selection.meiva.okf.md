---
type: doc
title: Pre-push frontend selection
description: "How the frontend pre-push hooks select work from the branch contribution: the merge-base range, the disk and type inputs outside Vitest's import graph, and the fixed repo-local commands they run."
tags:
- scripts
- ci
---

# Pre-push frontend selection

`scripts/provision/prepush_frontend.py` runs the frontend checks over the
branch contribution, never an old push tip. Vitest's import graph is only one
source of consumers: existing filesystem contract tests stay explicit, while
global configuration and deleted-module closure belong to CI and are reported
as unverified locally rather than running a full suite.

```meiva-contract
id = "branch-contribution-scope"
kind = "invariant"
statement = """The selection range is the branch contribution — merge-base(origin/main, HEAD)..HEAD, resolved by scripts/hooks/prepush-base.sh — never an old remote push tip. Renames and deletions keep both sides visible: --no-renames splits a rename into its deletion and addition. An unknown range is an error, never an empty change set."""
code = ["scripts/provision/prepush_frontend.py:contribution"]
tests = ["tests/ci/test_prepush_selection.py::test_new_branch_runs_own_source_and_disk_consumer", "tests/ci/test_prepush_selection.py::test_deleted_and_renamed_inputs_remain_visible", "tests/ci/test_prepush_selection.py::test_missing_base_is_not_no_changes"]
```

```meiva-contract
id = "type-artifact-inputs"
kind = "invariant"
statement = """_TYPE_INPUTS names the canonical schema/type artifacts without runtime imports (openapi.json, types-generated.ts): their consumers are proven by project tsc and the codegen-freshness hook, so a change confined to them schedules no related Vitest run while a changed runtime source still does."""
code = ["scripts/provision/prepush_frontend.py:_TYPE_INPUTS"]
tests = ["tests/ci/test_prepush_selection.py::test_schema_type_artifacts_keep_contract_checks_without_related_run", "tests/ci/test_prepush_selection.py::test_type_artifact_does_not_hide_changed_runtime_source"]
```

```meiva-contract
id = "disk-consumer-inputs"
kind = "invariant"
statement = """_DISK_CONSUMERS pairs each input read from disk outside Vitest's import graph (fixtures, locale files, css, layout, gate html) with the contract test that proves it; a new out-of-graph reader must join the table or its push-stage verification silently disappears. The input side is matched by prefix against the contribution paths."""
code = ["scripts/provision/prepush_frontend.py:_DISK_CONSUMERS"]
tests = ["tests/ci/test_prepush_selection.py::test_known_filesystem_consumers_run_without_import_edge", "tests/ci/test_prepush_selection.py::test_global_inputs_report_ci_gap_and_keep_known_disk_test"]
```

```meiva-contract
id = "empty-related-collection-is-not-green"
kind = "invariant"
statement = """Vitest's `related` mode defaults passWithNoTests=true; the selector overrides it to false: an empty affected collection is missing evidence, never a passing test run."""
code = ["scripts/provision/prepush_frontend.py:_vitest"]
tests = ["tests/ci/test_prepush_selection.py::test_empty_or_failed_related_run_stays_failed"]
```

```meiva-contract
id = "fixed-command-execution"
kind = "invariant"
statement = """Every process this selector runs is a fixed repo-local command — git queries and scripts/hooks/prepush-*.sh — with repo-owned path arguments and never a shell, so no untrusted input reaches argv (S603 is exempted for this file in pyproject.toml on that ground). The heavy-tool invocation is deliberately unbounded — the developer watches it run — and prepush-guard.sh serializes one heavy tool across host worktrees and reports skips loudly instead of failing silently."""
code = ["scripts/provision/prepush_frontend.py:_query", "scripts/provision/prepush_frontend.py:_run"]
tests = ["tests/ci/test_prepush_hooks.py::test_vitest_binary_and_command_status", "tests/ci/test_prepush_hooks.py::test_real_command_status_propagates"]
```
