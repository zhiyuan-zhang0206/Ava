---
type: doc
title: Native process primitives
description: Native process identity, observation, signaling and root control.
tags: [base]
---

# Native process primitives

`base/native_process/` owns native process identity, observation, signaling and root control.
Its component nodes describe the current contracts and implementation.

## Platform facts

`os_platform.is_linux()` reads Python's `sys.platform` directly without retaining a
process-level copy. The macOS and Windows compatibility flags remain available.
Disk sampling uses the macOS data volume on macOS and `/` on other POSIX hosts,
including WSL; no WSL detection or `uname` probe runs when the module imports.
Loaded commit and code version are separate boot facts and retain their frozen
process-generation contract.

## Documented components

- [[base/native_process/docs/group-closure.ava.okf.md]] — Process-group closure core.
- [[base/native_process/root_control/docs/root_control.ava.okf.md]] — Root control contract — protocol and Unix-socket client.
