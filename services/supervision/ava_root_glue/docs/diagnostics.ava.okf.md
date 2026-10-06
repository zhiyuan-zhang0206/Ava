---
type: doc
title: Root diagnostic observations
description: Capability-gated observations, bounded workers, and existing alert reporting without service recovery authority.
tags:
- ops
- healthchecks
---

# Root diagnostic observations

`diagnostic_probes.build_diagnostics` names the host checks explicitly. They run
concurrently within a finite roster; each check retains at most one worker.
`ProbeRunner` reports unexpected exceptions and deadline expiry as UNAVAILABLE.
It retains an overdue worker instead of creating replacements, discards its late
result, and requires a fresh observation before reporting health. Daemon workers
do not prevent root shutdown. Default observation cadence is 60 seconds and the
wait bound is 20 seconds; browser reachability retains its configured cadence and
larger protocol budget. The service monitor uses the same bounded observation
mechanism and remains the sole application retry scheduler. Initial failures
remain visible while each captured generation receives its declared startup
grace; first readiness or the deadline ends that grace.

| Diagnostic | Registration | Evidence |
|---|---|---|
| `brew-pin` | macOS | Bounded successful Homebrew installed/pinned queries; missing approved pins are DOWN, failed queries are unavailable. No package changes. |
| `venv` | POSIX | This executing checkout's dependency metadata and isolated `-I -B` imports of Ava, Pydantic, Psycopg, and FastAPI. Each command has a 5-second bound; no inherited PYTHONPATH or VIRTUAL_ENV. Missing uv is unavailable. |
| `permissions-helper` | Every macOS root | Connected Unix peer and ping PID must match the live native parent of root, with birth/lineage rechecked after reply; failures retain launchd classification. One `permissions_helper_unhealthy` event per episode; no helper bootout, bootstrap, or upgrade. |
| `redis-acl` | gateway with local data plane | Runtime-credential authenticated PING. No ACL writes or instance start. |
| `pgbouncer` | gateway with local enabled pooler | Admin-console authentication and required public listener. It does not mistake a failed Postgres query for a dead pooler. No ensure or restart. |
| `browser-reach` | selected root browser unit | Temporary browser fetch, contrasted with a host HTTP request. Failed host baseline or unusable CDP is unavailable; browser-only failure is DOWN. |
| `observatory-station` | gateway with remote observability URL | Authenticated remote OTLP protocol request. Missing credential or unresolved target is unavailable. A failure is episode-logged only: the station hosts the observability backends, so no alert can carry it. |
| `lgtm-write-path` | selected root Loki unit | Unique Loki write/read probe. Failed and persistently throttled paths retain their registered events; no backend restart. |

Redis and PgBouncer diagnostics use their configured endpoints and existing
credentials without native process custody inspection. Neither diagnostic
inherits a maintenance stop verb or imports `cli`; authenticated read-only
protocol observations cannot start resources or repair credentials.

Status includes expected diagnostics before any sample, with null sample time and
verdict. Each subsequent sample includes its verdict, detail, and the consecutive
non-alive count: failures and unknown results both extend the streak, and only a
healthy sample resets it. Every non-alive sample emits `root_diagnostic` at
WARNING (the observability rule's pending period carries the debounce) and the
first healthy sample after a streak logs the recovery at INFO. Alert reporters
run only on fresh, non-unavailable evidence through the same bounded worker
slot. An alert backend that hangs cannot create concurrent reports or
diagnostic workers.

Readiness of native LGTM services belongs to their actual root service specs.
External station reachability is a remote protocol claim and carries no authority
over that station's native processes. [[services/supervision/ava_root_glue/docs/ava_root_glue.ava.okf.md|Root wiring]]
keeps these ownership boundaries explicit.
