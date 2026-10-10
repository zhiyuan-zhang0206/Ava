---
type: doc
title: "Page-server process composition"
description: "The page-server entry supplies one captured image and an explicit database factory to logging, health and the two resident loops."
tags:
- services
- pages
---

# Page-server process composition

`services.agent_runner.page_server.daemon.main` captures one `LoadedCommit`
before its existing schema check. The same image supplies logging, health and
its `CodeVersion`; the latter resolves only the captured source root/SHA.
Its non-exempt `ProcessDbGate` retains the `page_server` process name. An
unknown image stays unknown and cannot borrow a later checkout's version.

One entry-owned database factory reads the current settings and passes that
gate to each handle. The entry supplies its logging pipeline, validated live
machine reader, database factory and image explicitly. `run(database=...,
image=...)` still publishes its pidfile and health endpoint before creating the
pool, then supplies that same work handle to the dead-page loop.

The existing page-server config and event bus stay at their original run
boundary. One TaskGroup owns reconciliation and dead-page scanning, with
separate progress trackers: a crashing loop cancels its sibling. Cleanup keeps
its original order: stop health, close the pool, remove the pidfile. Signal
handling, bounded cancellation drain and hard exit keep their existing entry
behavior; this composition does not change page leases or child resources.
