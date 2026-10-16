---
type: doc
title: Fleet-update test harness
description: The in-memory cluster, the fixtures and the keep-decision stub the cli.fleet_update tests run against.
tags: [cli, updates, testing]
---

# Fleet-update test harness

`cli/tests/fleet_update/` proves `cli.fleet_update` without SSH, a shell or
launchd. `fleet_update_cluster.py` holds the in-memory `Cluster` each test
drives; `conftest.py` builds the shared `env` fixture on top — a real git
repository with an OLD and a NEW commit, `fleet_update.ssh` patched to the
cluster, `fleet_update.time` patched to its fake clock. `test_fleet_update.py`
covers the halves' behavior, `test_fleet_update_keep.py` the keep decision's
effect on `down`, and `test_fleet_update_programs.py` runs the shipped
`cli/commands/management/fleet_gateway.py` program for real against a local
HTTP server standing in for the gateway API.

The cluster records every command and every effect it accepts, and the order
of those effects is what the tests assert. `hosts`, `names`, `silent`,
`hidden`, `roster_lag`, `stored_schedules`, `silence_reply` and `fail` are
per-test knobs; the fake clock makes the roster's poll interval observable.

Tests never run the real keep-decision script. The shared fixture stubs a
decision that keeps nothing — the full-stop shape the pre-keep tests expect —
and a test that cares writes its own payload with `stub_decision`, which
records each invocation's argv by appending to `<script>.argv`, so a test can
prove the decision ran, over which revisions, or did not run at all.

## Contracts

```ava-contract
id = "harness-order-guards"
kind = "invariant"
statement = "The in-memory cluster refuses production's forbidden order at the offending command — a gateway stop or start while a runner is up, a runner start before the gateway, a switch before its stop, a macOS start that is not the one-shot LaunchAgent — and the pre-verify simulation accepts only the gateway, only against NEW, with the table read before the worktree check and the check before any stop of the run."
code = ["cli/tests/fleet_update/fleet_update_cluster.py:Cluster", "cli/tests/fleet_update/fleet_update_cluster.py:Cluster._effect", "cli/tests/fleet_update/fleet_update_cluster.py:Cluster._verify"]
tests = ["cli/tests/fleet_update/test_fleet_update.py::test_runners_stop_first_and_start_last", "cli/tests/fleet_update/test_fleet_update.py::test_the_first_failure_stops_the_half", "cli/tests/fleet_update/test_fleet_update.py::test_stored_schedules_are_checked_against_new_before_anything_stops"]
```

```ava-contract
id = "keep-decision-stub"
kind = "invariant"
statement = "Tests never run the real keep-decision script: the stub writes its payload into the fake checkout and appends each invocation's argv to a sibling file, so a test can tell whether the decision ran and over which revisions."
code = ["cli/tests/fleet_update/fleet_update_cluster.py:stub_decision", "cli/tests/fleet_update/fleet_update_cluster.py:decision_argvs", "cli/tests/fleet_update/conftest.py:env"]
tests = ["cli/tests/fleet_update/test_fleet_update_keep.py::test_the_decision_judges_the_unswitched_head_and_a_rerun_judges_nothing", "cli/tests/fleet_update/test_fleet_update_keep.py::test_restart_all_skips_the_decision", "cli/tests/fleet_update/test_fleet_update_keep.py::test_a_missing_decision_script_stops_everything"]
```

Parent: [[cli/docs/cli.ava.okf.md]].
