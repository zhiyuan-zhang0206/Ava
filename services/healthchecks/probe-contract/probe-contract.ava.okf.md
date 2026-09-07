---
type: doc
title: Probe contract — a healthcheck always returns a verdict
description: The rule every healthcheck probe obeys — return alive-or-dead, never raise — and the total-wrapper pattern that enforces it structurally rather than by widening catch tuples. Generalized to the whole main(), where nothing failable may sit ahead of the respawn. Includes the browser healthcheck's two-probe rule and redis-acl's documented exception.
tags:
- ops
---

# Probe contract — a healthcheck always returns a verdict

## The rule

A probe returns **alive or dead**. It never raises.

The watchdog isolates each check (`_run_check` catches `Exception`), so a raising probe does **not** take the round down — which is exactly why this was invisible for so long. What it does instead is worse in a quiet way: the service is never judged alive-or-dead, so **no restart is ever attempted**, while every 60s round writes a fresh multi-KB traceback.

`browser_mcp.py` ran in this state on the `win` runner. Its `_probe` names `socket.AF_UNIX`, which does not exist on Windows, so it raised `AttributeError` — a type `except (OSError, json.JSONDecodeError)` does not catch. Nothing was ever revived, and `agent-runner-watchdog.log` reached **50 MB**. A health probe that silently converts "unknown" into log spam until it fills a disk is a failure mode worth naming on its own.

## Enforced by a total wrapper, not by wider catch tuples

The exception types a probe can hit are an open set — `urlopen`/`read` raise `http.client.HTTPException` (malformed status line, truncated body) which is not an `OSError`; a pidfile read can raise `PermissionError` from a frame the probe's own `try` does not cover. Widening tuples is whack-a-mole against that set.

So each probe is split in two, and the **public name is the total wrapper**:

| Wrapper (total) | Inner probe |
|---|---|
| `shared.daemon_health.probe_daemon` | `_probe_daemon` |
| `shared.daemon_health.probe_home` | `_probe_home` |
| `services.browser.probe.probe_browser` | `_probe_browser` |

The inner probe keeps its narrow catches — those produce the useful operator-facing `detail` strings ("healthz unreachable: ConnectionRefusedError", "identity mismatch…"). The wrapper turns anything they miss into `DaemonProbe.down("probe raised <Type>: …")` plus a logged traceback, so nothing is swallowed silently.

**It fails closed.** An unreadable probe is reported *down*, never *alive*: reporting alive would make the watchdog skip a genuinely dead daemon forever, which is the same 98-minute outage shape `probe_daemon`'s identity check exists to prevent.

`probe_daemon` is shared by daemon healthchecks, including agent-host, ops, heartbeat, indexing, messaging and backup services, so a single escaping exception type can silence their revival at once. `probe_home` is its pid-less sibling for the gateway (uvicorn reload makes the pid comparison unusable — a healthy gateway answers from a worker forked out of the process that wrote the pidfile), and it is where losing the verdict costs most: the gateway is what the cluster health probe polls, with `--auto-rollback --threshold 3` armed against it.

All three wrappers are **shared with the operator surfaces** through `ServiceSpec.identity_probe`, so `ava status` / `ava cluster health-probe` cannot believe a 2xx the shared watchdog probe rejected ([[services/healthchecks/terminal-verdict/terminal-verdict.ava.okf.md]]). im_bridge's watchdog is the one narrower action-policy exception: after a respawnable verdict it directly re-reads the same `/healthz`, and a matching `name` + `home` vetoes respawn even for a stale 503 or pidfile mismatch. Operator surfaces retain the strict shared verdict; only the watchdog's destructive action is suppressed.

Those three are total by construction; a **plugin-registered** `identity_probe` is only total by its author's discipline. So `cli/commands/_probe.py:_probe_service` catches around the call and reports `identity probe raised <Type>: <msg>` as the row's detail — failing closed, same as the wrappers, and naming the broken probe rather than flattening it into "down". The watchdog isolates each check per round; this is the same isolation for the surface an operator reaches for *because* the unit is already misbehaving, where one plugin's unhandled exception would otherwise cost the whole status screen.

`browser_mcp.py` follows the same rule inline, since its probe returns `bool` rather than `DaemonProbe`.

## The same rule one layer out

The probe is not the only place a healthcheck can lose its restart — the rule generalizes to the whole `main()`: [[services/healthchecks/probe-contract/main-ordering.ava.okf.md]].

## browser's two questions

[[services/healthchecks/probe-contract/browser-two-questions.ava.okf.md]]

## The documented exceptions: the two data-plane repairs

`redis_acl.check` raises on purpose when the **repair** path fails — `_start_redis`
returned non-zero, admin auth was rejected, or the repaired Redis still cannot
authenticate the cluster identity. Its PING distinguishes a reachable Redis that
lost its in-memory ACL from a dead local Redis; the latter reuses the same
idempotent bring-up as `ava start`, then verifies PING. It lets the watchdog log a
failed repair rather than deciding what to swallow.

`pgbouncer.check` raises on the same grounds and for the same reason: `ensure_pgbouncer` returned non-zero, or after the restart either probe still fails — the loopback admin console does not answer, or the reachable-address listener is still missing (a degraded double bind; task #1288). A pooler that cannot be brought back leaves the cluster with no pooled database front door, and that belongs on the operator's screen every round rather than in a swallowed verdict. Its protocol probe is narrower than a liveness probe on purpose — the admin console, never the end-to-end `SELECT 1` — so that a Postgres outage cannot be misread as a dead pooler and answered with a restart every 60s. The second probe (`pgbouncer_public_listener_reachable`) reads the local socket table for the configured reachable address or an IPv4/IPv6 wildcard. It does not self-dial through host networking: hairpin filtering can reject that route while the bind remains healthy. PgBouncer treats a failed bind on one `listen_addr` entry as a WARNING and keeps running on the rest, so the socket-table check still detects the loopback-only degraded state without turning a routing failure into a destructive restart.

## Key Dependencies
- [[services/healthchecks/terminal-verdict/terminal-verdict.ava.okf.md]] — the next question after "what is the verdict": whether a respawn can act on it
- [[services/healthchecks/healthchecks.ava.okf.md]] — the per-service probe/restart table
- [[services/watchdog/watchdog.ava.okf.md]] — the caller that isolates each check per round
