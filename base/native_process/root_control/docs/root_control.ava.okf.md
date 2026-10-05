---
type: doc
title: "Root control contract — protocol and Unix-socket client"
description: "The client side of the ava-root supervisor below every consumer: the JSON-line wire protocol and the blocking RootClient, which validates status against the kernel-reported Unix-socket peer identity."
tags:
- base
- lifecycle
- ipc
---

# Root control contract

`base/native_process/root_control/` holds what both ends of the root supervisor's local
control plane speak, so lower layers reach the root without importing the
service ([[services/supervision/ava_root/docs/ava_root.ava.okf.md]] is the server).

- `ipc.py` — one JSON object per line, capped at 64 KiB; requests and responses
  are validated fail-fast, unknown verbs and error codes are rejected. It owns
  `UnitState` (`running` / `stopped`); successful status replies require a unit
  roster with a valid id and known state on every row.
- `client.py` — `RootClient`, one blocking connection per call. A `status`
  reply must name the kernel-reported Unix-socket peer; `root_process` and
  `owned_process` read captured
  native births and never adopt a current PID occupant. A missing or unknown
  unit state rejects the reply. An absent unit, a validated stopped state, or
  a positively dead/reused native birth establishes that the recorded
  generation is not running.

Consumers below the service: the start-serving gate (`base/deploy/lifecycle/start_serving.py`)
authenticates the live root generation through `RootClient.status()`.
Import-linter's "base must not import services" contract keeps this direction.
