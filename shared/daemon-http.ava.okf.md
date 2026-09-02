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
