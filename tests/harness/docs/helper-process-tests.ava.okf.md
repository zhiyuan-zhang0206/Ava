---
type: doc
title: Helper process contract tests
description: Socket-free spawn doubles, durable ownership and real ancestor-chain probes.
tags: [tests, process, helper]
---

# Helper process contract tests

### Helper spawn double

```ava-contract
id = "helper-spawn-double"
kind = "invariant"
statement = "The helper spawn double records argv, environment and output destinations without opening a helper socket; helper refusal remains loud and has no fallback launch."
code = ["tests/harness/test_helperproc.py:_SpawnClient.spawn_process"]
tests = [
  "tests/harness/test_helperproc.py::test_new_session_preserves_login_shell_env_stderr_and_record",
  "tests/harness/test_helperproc.py::test_spawn_failure_is_loud_and_has_no_fallback",
]
```

The test double accepts variable arguments to forward the production call unchanged
to its scenario callback. Its existing `reportIncompatibleMethodOverride` exception
is confined to that test override; the real client signature remains authoritative.

### Helper record and identity

```ava-contract
id = "helper-record-and-identity"
kind = "invariant"
statement = "Durable session records own spawn idempotency. Final graceful delivery accepts the same birth-time tolerance as resolution and rejects a birth outside that tolerance."
code = [
  "tests/harness/test_helperproc.py:test_new_session_preserves_login_shell_env_stderr_and_record",
  "tests/harness/test_helperproc.py:test_graceful_delivery_tolerates_whole_second_create_time_drift",
]
tests = [
  "tests/harness/test_helperproc.py::test_new_session_preserves_login_shell_env_stderr_and_record",
  "tests/harness/test_helperproc.py::test_graceful_delivery_tolerates_whole_second_create_time_drift",
  "tests/harness/test_helperproc.py::test_graceful_delivery_refuses_a_birth_beyond_the_tolerance",
]
```

A helper's volatile nursery table cannot replace the persistent record. The
whole-second drift case preserves the macOS boot-time correction rationale:
resolution and final signal delivery must agree about the accepted birth.

### Helper ancestor chain

```ava-contract
id = "helper-ancestor-chain"
kind = "invariant"
statement = "A root supervisor between a unit and the permissions helper does not break the helper ancestry check; malformed markers, broken intermediate links and an unrelated helper are rejected."
code = ["tests/harness/test_helperproc.py:test_parent_chain_guard_accepts_the_helper_above_an_intermediate"]
tests = [
  "tests/harness/test_helperproc.py::test_parent_chain_guard_accepts_the_helper_above_an_intermediate",
  "tests/harness/test_helperproc.py::test_parent_chain_guard_rejects_a_broken_intermediate_link",
  "tests/harness/test_helperproc.py::test_parent_chain_guard_allows_unmanaged_and_rejects_malformed_markers",
  "tests/harness/test_helperproc.py::test_parent_chain_guard_rejects_a_helper_outside_the_chain",
]
```

The former direct-parent assumption mistook `unit -> root -> helper` for an
orphan and terminated the unit during boot. The tests preserve that geometry.

### Helper probe executable

```ava-contract
id = "helper-probe-executable"
kind = "invariant"
statement = "The real ancestor-chain probe launches the current interpreter with a fixed repository probe script and test-owned paths, then checks the reported ancestry for an intact or unrelated helper."
code = ["tests/harness/test_helperproc.py:_run_probe_chain"]
tests = [
  "tests/harness/test_helperproc.py::test_parent_chain_guard_on_a_real_process_chain",
  "tests/harness/test_helperproc.py::test_parent_chain_guard_rejects_an_outside_pid_on_a_real_chain",
]
```

The `S603` exception belongs only to this fixed test executable. It does not
admit operator-supplied executable text, and the call retains its 60-second bound.
