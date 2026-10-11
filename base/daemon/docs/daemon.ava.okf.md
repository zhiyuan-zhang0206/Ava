---
type: doc
title: Daemon contracts
description: Shared daemon HTTP transport, health contracts and schedule vocabulary.
tags: [base]
---

# Daemon contracts

`base/daemon/` owns shared daemon HTTP transport, health contracts and schedule vocabulary.
Its component nodes describe the current contracts and implementation.

`ServiceEndpoints.from_boot(config, runtime=...)` constructs a daemon endpoint
table from a required configuration owner and runtime directory. Port fields
remain live until each table is built; an existing table retains its captured
values. The registered health-port resolver owns default ports and unknown-name
failures for both this factory and the remaining legacy `from_settings` roots.
The factory does not create a boot or infer a home. Process roots own runtime
directory creation at their existing pidfile/health resource decision.

## Documented components

- [[base/daemon/docs/http-transport.ava.okf.md]] — Settings-free daemon HTTP transport.
- [[base/daemon/schedules/docs/completion-notices.ava.okf.md]] — Completion Notice Vocabulary.
