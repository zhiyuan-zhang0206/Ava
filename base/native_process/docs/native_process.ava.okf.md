---
type: doc
title: Native process primitives
description: Native process identity, observation, signaling and root control.
tags: [base]
---

# Native process primitives

`base/native_process/` owns native process identity, observation, signaling and root control.
Its component nodes describe the current contracts and implementation.

`turn_identity` carries the original native admission and hosted-resource scope.
It exposes no current-agent getter; graph identity comes from `Runtime[AvaContext]`
and SDK identity from the execution process entry. Native incarnation/task-copy
and resource-settlement fences remain distinct from ordinary log attribution.

## Platform facts

`os_platform.is_linux()`, `os_platform.is_macos()` and `os_platform.is_windows()`
read Python's `sys.platform` directly without retaining process-level copies.
Platform-dependent socket directory and length choices are made when the socket
is requested, rather than cached when its owner module imports.
Disk sampling uses the macOS data volume on macOS and `/` on other POSIX hosts,
including WSL; no WSL detection or `uname` probe runs when the module imports.
Loaded commit and code version are separate boot facts and retain their frozen
process-generation contract.

## Documented components

- [[base/native_process/docs/group-closure.ava.okf.md]] — Process-group closure core.
- [[base/native_process/root_control/docs/root_control.ava.okf.md]] — Root control contract — protocol and Unix-socket client.
