---
type: doc
title: "Root control contract — protocol, client and native transports"
description: "The client side of the ava-root supervisor below every consumer: the JSON-line wire protocol, the blocking RootClient with kernel-reported peer identity, and the native Windows pipe, custody-file and owner-only security primitives both ends use."
tags:
- shared
- lifecycle
- ipc
---

# Root control contract

`shared/root_control/` holds what both ends of the root supervisor's local
control plane speak, so lower layers reach the root without importing the
service ([[services/ava_root/ava_root.ava.okf.md]] is the server).

- `ipc.py` — one JSON object per line, capped at 64 KiB; requests and responses
  are validated fail-fast, unknown verbs and error codes are rejected.
- `client.py` — `RootClient`, one blocking connection per call. A `status`
  reply must name the kernel-reported peer (Unix socket credentials, or the
  Windows pipe server PID); `root_process` and `owned_process` read captured
  native births and never adopt a current PID occupant.
- `windows/transport.py` — the owner-only local named pipe, both ends. The root
  control server and each Windows terminal owner serve on it.
- `windows/storage.py` — the exclusive singleton lock handle and write-through
  custody publication used by root custody and terminal records.
- `windows/native.py` — current-user security descriptors and native command
  line parsing.

Consumers below the service: the start-serving gate (`shared/start_serving.py`)
authenticates the live root generation through `RootClient.status()`, and the
Windows terminal backend (`shared/windows_terminal/`, dispatched by
`shared/session_backend.py`) requests terminal births from the root and queries
each terminal owner over its own pipe. Import-linter's "shared must not import
services" contract keeps this direction.
