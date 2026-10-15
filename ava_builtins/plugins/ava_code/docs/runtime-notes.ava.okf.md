---
type: doc
title: Ava Code runtime state and notes
description: Host-side state deltas, after-exec messages and logical cwd recovery.
tags: [plugins, runtime, contracts]
---

# Runtime state and notes

The agent-runtime face is loaded for host-side hooks. The SDK face is owned by
the plugin entry and its state-access helpers; hooks receive graph state rather
than reading the SDK execution slot. The plugin overview owns registration and
the separation from context-file injection.

```ava-contract
id = "after-exec-messages"
kind = "invariant"
statement = "The after-exec hook returns a messages delta containing pending cwd and project-skills notes. It consumes cwd_note and emits the project-skills note once per compaction version; without pending notes it returns None."
code = ["ava_builtins/plugins/ava_code/agent_runtime.py:_InjectCwdNotesAfterExecHook"]
tests = [
  "ava_builtins/plugins/ava_code/tests/test_after_exec_hook.py::test_after_exec_hook_injects_cwd_and_project_skills_notes",
  "ava_builtins/plugins/ava_code/tests/test_after_exec_hook.py::test_after_exec_hook_reinjects_project_skills_only_after_compact",
  "ava_builtins/plugins/ava_code/tests/test_after_exec_hook.py::test_after_exec_hook_is_noop_without_pending_notes",
]
```

The hook reads the supplied state through the state handle's view and returns
writes through its delta. Context-file injection runs separately in the exec path.

```ava-contract
id = "logical-cwd"
kind = "invariant"
statement = "After initialization, a valid persisted logical cwd is preserved. An absent or unusable cwd is replaced by the invoking agent's workspace, or home when no agent identity exists. This does not change the Python process cwd."
code = ["ava_builtins/plugins/ava_code/agent_runtime.py:ValidateCwdAfterInitHook"]
tests = [
  "ava_builtins/plugins/ava_code/tests/ava_code_plugin/test_read_wrap_hash_dedup_primary_path_blocks.py::test_after_init_initializes_each_host_workspace_without_sdk_slot",
  "ava_builtins/plugins/ava_code/tests/ava_code_plugin/test_read_wrap_hash_dedup_primary_path_blocks.py::test_after_init_no_agent_initializes_home",
  "ava_builtins/plugins/ava_code/tests/ava_code_plugin/test_read_wrap_hash_dedup_primary_path_blocks.py::test_after_init_hook_falls_back_when_cwd_missing",
  "ava_builtins/plugins/ava_code/tests/ava_code_plugin/test_read_wrap_hash_dedup_primary_path_blocks.py::test_after_init_hook_noop_when_cwd_valid",
  "ava_builtins/plugins/ava_code/tests/ava_code_plugin/test_read_wrap_hash_dedup_primary_path_blocks.py::test_after_init_hook_falls_back_when_cwd_is_file",
]
```

SDK file, shell, understanding and UI wrappers resolve their paths against the
logical cwd. Bare Python file operations and subprocess calls retain the
process's startup cwd. Initialization uses the invocation's explicit runtime
identity and distinguishes an absent field from a deliberately persisted value.
