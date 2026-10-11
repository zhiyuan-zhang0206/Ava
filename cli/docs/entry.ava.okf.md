---
type: doc
title: CLI entries
description: Fresh executable logging and embedded command dispatch.
tags:
- gateway
- tool
---

# CLI entries

- `cli/main.py:entry()` — fresh executable logger preparation; `main()` — embeddable dispatch preserving caller sinks; `cli.parsers` — `build_parser` for construction, `parse_args` for syntax, `command_options` for documentation.
- `cli/init_intent.py:run_init()` — the first-start inputs; `cli/start_intent.py:run_start()` — start admission and the full home lifecycle lock; `cli/start_identity.py` — durable initialization journal and the start gate (`require_initialized`); `cli/unit_join.py` — a runner's gateway join.
- `cli/commands/cluster/home.py` — `destroy` (confirmed at a terminal), exact cleanup before marking the home detached; `start.py` / `status.py` / `data_plane/cluster_instance.py` — runtime operations.


The console script and both Python module entry paths invoke `entry()` once.
It calls `base.log.prepare_process_logging()` before dispatch loads configuration.
That operation deliberately removes sinks only for a fresh executable process.
Ordinary imports and embedded `main()` calls preserve caller logging; repeated
concrete logger initialization retains its original first-role guard.
