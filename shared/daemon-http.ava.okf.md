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

The settings-free boundary supports explicit read-only observation tools without
loading ordinary daemon configuration. The transport does not validate a release,
prove process closure or grant startup permission; callers establish their own
admission before binding.

`shared.process_evidence` owns strict digest/model values, exact native process
identity and read-only process observations. Runtime image construction, service
health and native launcher reads import these values without loading rollout
leases or managed-writer controllers. Native read errors remain unknown; PID
reuse stays distinct from the expected process exiting. This evidence grants no
startup or mutation authority.

`shared.managed_writer_observation` adds session and launcher facts for the
prepared inventory producer. Its `UnitObserver` route
checks an outstanding challenge before and after off-loop OS reads. Exact live
processes, exited processes and reused PID identities remain distinct. Session
records that are malformed, unreadable, substituted or still present never
become absent by convenience. No signal or session mutation is performed.

The normal ops daemon has no bootstrap observation mode, restricted `/ops`
allowlist or one-shot dispatch child. Unknown daemon arguments, including the
retired bootstrap option, refuse before ordinary imports. The generic transport
mounts only routes explicitly supplied by a caller.

## Native launcher observation

`shared.native_job_observation` reuses the existing native user-crontab and
launchd surfaces without importing Settings or registering jobs. Expected launchd
identity is the plist Label plus SHA256 of its raw bytes; cron identity is SHA256
of the exact job line without its newline. Reads are bounded by the outstanding
challenge and repeated to reject observed drift: both the definition bytes and the
loaded verdict must be identical across the two reads, and a verdict that changes
in any way (including to `None`, which can hide a changed enumeration) refuses
rather than falling back to the first read. Raw definitions and command
output are never returned or logged. A definition that is absent is a reported
fact, not an error — absence is the file's own lstat ENOENT, read twice for
stability; foreign bytes are summarized by digest only, never parsed.

Definition/home/prepared-image binding describes the **on-disk declaration**, not
the scheduler's loaded executable. The loaded verdict is three-valued
(`launchd_loaded_state`): `True` is the exact launchctl lookup answering; `False`
additionally requires two equal, stable GUI enumerations excluding the label;
`None` is unknown and must never be read as "not loaded", because launchctl
errors do not prove absence. A declared non-Aqua `LimitLoadToSessionType` is
unobservable in the GUI domain and degrades to unknown. Effective launchd enabled
overrides and loaded-image identity remain unknown: Apple documents
`launchctl print` output as diagnostic, not an API, and `list -x` is unsupported.
Linux `enabled` means an exact non-commented cron registration exists, not that
the cron daemon is live; cron facts never carry a loaded verdict (no separate
scheduler state exists), and the closure fence treats the double-read table
fact as complete for removals while refusing crontab rebound claims. Every
observation names its scheduler family (`kind`, filled by the producing
observer itself); the fence branches on it and refuses cross-family shapes.
Missing/unsupported/unreadable/drifting evidence remains unknown; Windows is
unsupported. Empty input never proves fleet closure.

CI separately exercises real read-only `crontab -l` and launchctl queries on native
runners. Parser fixtures do not prove effective scheduler state. The observer
still emits `closure=unknown`, and that is permanent semantics rather than a
placeholder: a positive `old_writers_absent_relaunchers_fenced` literal is
derived where these observed facts meet the hop ledger (`shared.managed_writer_closure`,
an inert tested seat), never by the observer itself; updater/adoption activation is
not implemented.
