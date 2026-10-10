---
type: doc
title: Settings-free daemon HTTP transport
description: Shared bounded HTTP parsing, bearer authentication and explicit route serving without Settings initialization.
tags:
  - base
  - transport
---

# Settings-free daemon HTTP transport

`base.daemon.http_transport.start_daemon_http` is the HTTP transport used by
`base.daemon.health.start_health_server`. It accepts an explicit address,
port, health response, route map and authentication token. It never resolves
configuration, creates a home, writes a PID or registers a unit.

Normal daemons retain their existing health wrapper: configuration-derived
port, resolved home, process identity and aggregated liveness payload. Unknown
routes return 404; configured bearer authentication precedes explicit route
execution; unauthenticated health responses contain no secrets. Parsing and
body limits remain shared rather than independently reimplemented.

The settings-free boundary supports explicit read-only tools without loading
ordinary daemon configuration. The transport does not validate a release,
prove process closure or grant startup permission; callers establish their own
admission before binding.

`base.native_process.evidence` owns strict digest/model values and the exact
native process identity. Service health imports them without loading rollout
leases. This evidence grants no startup or mutation authority.

The generic transport mounts only routes explicitly supplied by a caller.

The health wrapper accepts an optional immutable `LoadedCommit` from the process
entry point. Its handler retains that exact image and reports `image.sha` without
reading Git or the legacy capture. An explicit unknown stays unknown; callers
without an image keep their existing legacy source. The SHA enriches probe
version detail and does not determine readiness or authorize a restart.
