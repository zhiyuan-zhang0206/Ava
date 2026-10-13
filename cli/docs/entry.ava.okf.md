---
type: doc
title: CLI entries
description: Fresh executable logging and embedded command dispatch.
tags:
- gateway
- tool
---

# CLI entries

- `cli/main.py:entry()` — fresh executable logger preparation; `main()` — embeddable dispatch preserving caller sinks, which runs `run_command(argv, retained_children=, producer_factory=)` with the operator event pipeline as the producer factory (a caller that needs another producer, such as a different flush interval, calls `run_command` with its own factory); `cli.parsers` — `build_parser` for construction, `parse_args` for syntax, `command_options` for documentation.
- `cli/init_intent.py:run_init()` — the first-start inputs; `cli/start_intent.py:run_start()` — start admission and the full home lifecycle lock; `cli/start_identity.py` — durable initialization journal and the start gate (`require_initialized`); `cli/commands/cluster/unit_join.py` — a runner's gateway join.
- `cli/commands/cluster/home.py` — `destroy` (confirmed at a terminal), exact cleanup before marking the home detached; `start.py` / `status.py` / `data_plane/cluster_instance.py` — runtime operations.


The console script and both Python module entry paths invoke `entry()` once.
It calls `base.log.prepare_process_logging()` before dispatch loads configuration.
That operation deliberately removes sinks only for a fresh executable process.
Ordinary imports and embedded `main()` calls preserve caller logging; repeated
concrete logger initialization retains its original first-role guard.

`init` dispatches without configuration imports. `start` checks
`require_initialized` before constructing its root, then checks the admitted home
again under its lifecycle lock. Other parsed runtime commands create one
`cli.database.runtime.CommandRuntime` with a zero-read `ConfigBoot` and its lazy
`MachineIdentity`. Dispatch, embedded
lifecycle legs, logging, database host resolution and telemetry borrow that same
identity. Successful setup resets only this owner before role and intent checks;
failed setup preserves its cached inputs. Help exits before constructing the root,
and lightweight status/stop paths do not force identity or configuration fetches.
