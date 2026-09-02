---
type: doc
title: Settings-free daemon HTTP transport
description: Shared bounded HTTP parsing, bearer authentication and explicit route serving without Settings initialization.
tags:
  - shared
  - transport
---

# Settings-free daemon HTTP transport

`shared.daemon_http.start_daemon_http` is the HTTP transport used by
`shared.daemon_health.start_health_server`. It accepts an explicit address,
port, health response, route map and authentication token. It never resolves
configuration, creates a home, writes a PID or registers a unit.

Normal daemons retain their existing health wrapper: configuration-derived
port, resolved home, process identity and aggregated liveness payload. Unknown
routes return 404; configured bearer authentication precedes explicit route
execution; unauthenticated health responses contain no secrets. Parsing and
body limits remain shared rather than independently reimplemented.

The settings-free boundary permits a prepared ops observation mode to report
bootstrap-only health without importing the ordinary RPC mutation handlers.
The transport itself does not validate a prepared release, prove process/job
closure or grant startup permission. Its caller must establish these before
binding, and an observation-only mode must not report normal ops readiness.

`shared.managed_writer_observation` supplies typed expected process, session and
launcher facts for the prepared inventory producer. Its `UnitObserver` route
checks an outstanding challenge before and after off-loop OS reads. Exact live
processes, exited processes and reused PID identities remain distinct. Session
records that are malformed, unreadable, substituted or still present never
become absent by convenience. No signal or session mutation is performed.

The current observer returns `closure: unknown` unconditionally: platform job
observation, prepared-image/home/operation entry validation, actual updater
replacement and complete inventory production are not yet connected. An empty
test inventory is not evidence of complete unit or fleet closure. Normal ops
routes are not registered on the test observation socket.
