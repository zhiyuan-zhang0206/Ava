---
type: doc
title: Meiva Code runtime state and notes
description: Host-side state deltas, after-exec messages and logical cwd recovery.
tags: [plugins, runtime, contracts]
---

# Runtime state and notes

The agent-runtime face is loaded for host-side hooks. The SDK face is owned by
the plugin entry and its state-access helpers; hooks receive graph state rather
than reading the SDK execution slot. The plugin overview owns registration and
the separation from context-file injection.

The hook reads the supplied state through the state handle's view and returns
writes through its delta. Context-file injection runs separately in the exec path.

SDK file, shell, understanding and UI wrappers resolve their paths against the
logical cwd. Bare Python file operations and subprocess calls retain the
process's startup cwd. Initialization uses the invocation's explicit runtime
identity and distinguishes an absent field from a deliberately persisted value.
