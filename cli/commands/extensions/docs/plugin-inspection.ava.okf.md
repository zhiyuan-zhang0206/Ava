---
type: doc
title: Plugin inspection commands
description: Read-only inspect and verify output and load boundaries.
tags: [contracts]
---

# Plugin inspection commands

## Inspection output and verification

```ava-contract
id = "inspection-output-and-verification"
kind = "invariant"
statement = "inspect reports surfaces or one installed plugin and returns nonzero for an unknown name. verify loads the enabled agent-boot faces, exits 1 for contained plugin failures and 2 for a loader crash; it does not start an agent or invoke hooks."
code = [
  "cli/commands/extensions/plugins_inspect.py:cmd_plugins_inspect",
  "cli/commands/extensions/plugins_inspect.py:cmd_plugins_verify",
]
tests = [
  "cli/commands/extensions/tests/test_plugins_inspect.py::test_no_argument_lists_every_surface_and_every_plugin",
  "cli/commands/extensions/tests/test_plugins_inspect.py::test_an_unknown_plugin_exits_nonzero",
  "cli/commands/extensions/tests/test_plugins_verify.py::test_a_plugin_using_a_deleted_framework_api_is_red_and_the_rest_still_count",
  "cli/commands/extensions/tests/test_plugins_verify.py::test_a_loader_crash_is_a_tool_error_not_a_red",
  "cli/commands/extensions/tests/test_plugins_verify.py::test_nothing_is_emitted_while_verifying_and_the_canonical_reporter_is_unchanged",
]
```

Inspect output is line-oriented for both people and agents: stable tokens, one fact per line, and signatures kept unwrapped for grep. It links registration facts to activation telemetry without querying the event store. Verify reports `RESULT enabled=N failed=M rc=R`, then `RED plugin=...` for each contained failure or `TOOL-ERROR` for a loader failure. The reporter contains diagnostics without emitting telemetry; plugin import-time code still runs. Only the SDK, config and agent-runtime faces are verified: services, providers and data faces have separate load paths.
