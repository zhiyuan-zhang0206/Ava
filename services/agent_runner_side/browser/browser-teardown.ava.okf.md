---
type: doc
title: Browser teardown custody
description: Browser shutdown follows captured root process custody; profile similarity never grants takeover authority.
tags:
- browser
- ops
---

# Browser teardown custody

The browser service belongs to the selected root tree. Root records native PID,
birth identity, and descendant custody and must settle that ownership before a
replacement can start. Browser protocol and reachability probes cannot kill,
adopt, or restart processes.

A matching profile path identifies a possible conflict but cannot authorize a
kill. A browser outside the captured generation holding the CDP port is a
terminal ownership conflict. Unreadable native evidence is unavailable, rather
than proof that the port is free. Interactive tabs and persistent browser state
are governed by the explicit cluster lifecycle operation, not diagnostic failure.

[[services/healthchecks/probe-contract/browser-two-questions.ava.okf.md|Browser readiness]]
requires protocol evidence and the same native ownership boundary.
