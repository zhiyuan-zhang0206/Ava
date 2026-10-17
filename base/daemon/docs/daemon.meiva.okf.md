---
type: doc
title: Daemon contracts
description: Shared daemon HTTP transport, health, shutdown contracts and schedule vocabulary.
tags: [base]
---

# Daemon contracts

`base/daemon/` owns shared daemon HTTP transport, health, shutdown contracts and schedule vocabulary.
Its component nodes describe the current contracts and implementation.

`ServiceEndpoints.from_boot(config, runtime=...)` constructs a daemon endpoint
table from a required configuration owner and runtime directory. Port fields
remain live until each table is built; an existing table retains its captured
values. The registered health-port resolver owns default ports and unknown-name
failures for both this factory and the remaining legacy `from_settings` roots.
The factory does not create a boot or infer a home. Process roots own runtime
directory creation at their existing pidfile/health resource decision.

`cancel_and_drain(runner, interrupted_by=...)` cancels one snapshot of loop tasks
and joins one retained gather. A caller may identify the exact `KeyboardInterrupt`
it already caught: a TaskGroup replay of that same object resumes the existing
join without cancelling cleanup again. Another interrupt, `SystemExit`, and
unknown loop failures still propagate. The default `None` consumes no replay.
Ordinary cleanup exceptions retain their identities in the returned failure
list; callers report them and choose their error exit code. No executor join or
new cleanup deadline is introduced. A caller reaches `hard_exit` only after this
drain returns and its other owned cleanup has completed.

## Documented components

- [[base/daemon/docs/http-transport.meiva.okf.md]] — Settings-free daemon HTTP transport.
- [[base/daemon/schedules/docs/completion-notices.meiva.okf.md]] — Completion Notice Vocabulary.
