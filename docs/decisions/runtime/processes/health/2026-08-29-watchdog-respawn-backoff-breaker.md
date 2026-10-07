# Watchdog respawn: exponential backoff + circuit breaker instead of a fixed retry loop

Date: 2026-08-29

## Context

Three incidents of the same shape: a daemon dies of a condition a restart
cannot cure (2026-08 #920 ENOSPC crash-loop in upload_loop, #903/#3962
heartbeat context-overflow, #927 GCS-unreachable 2h+ making the gateway
DEGRADED → /healthz 503 → DOWN), and `run_keepalive` — the shared body of every
daemon healthcheck — respawned it once per 60s watchdog round forever:
kill + restart, restart fails, next round again. Each round also burned a full
respawn-and-verify window (~20s) and logged an ERROR. The loop was not
remediation; it was a 60s heartbeat of guaranteed failure.

## Decision

`run_keepalive` now schedules failed respawns with exponential backoff
(`base * 2^n`, base = watchdog round interval, cap = 30min via
`AVA_WATCHDOG_RESPAWN_BACKOFF_CAP_SECONDS`) and opens a circuit breaker after
`watchdog_respawn_breaker_rounds` (default 5) consecutive rounds without a
probe-alive verdict: respawns hold, each round WARNINGs the hold age, and
exactly one `respawn_breaker_open` event fires through the unified events
pipeline per episode. Any probe-alive round — or a verified-alive respawn —
fully resets failure count, backoff, and breaker.

A failed respawn no longer raises `SystemExit(EXIT_RESPAWN_FAILED)`: the round
reports the failure (WARNING naming the next attempt) and returns. The terminal
verdict (`PORT_TAKEN`, exit 3) is unchanged — it is the one exit code that still
means "a human must intervene". `on_unrevivable` ordering is unchanged (never
before a respawn attempt). The breaker must be configured strictly
greater than the respawn threshold; an equal or smaller value would hold without
(ever again) respawning — the breaker check runs before the threshold branch —
and is rejected at validation.

## Alternatives rejected

- **Keep the fixed per-round retry (status quo).** Rejected by the three
  incidents: for unrevivable conditions it is a pure failure loop that hides
  the actual condition behind constant ERROR churn and masks the outage in
  the watchdog log.
- **Exit 1 per failed respawn and let the watchdog pace it (exit-code
  contract).** The exit code made the watchdog log a failure per attempt
  round, which with backoff is sparse — but the round's state (backoff,
  breaker) lives in the watchdog process either way, so the exit carried no
  information the WARNING lines and the `respawn_breaker_open` event do not.
  Returning keeps the healthcheck a "still managing" signal instead of a
  repeated failure, and keeps the flap regression test a plain loop. The
  terminal exit code stays distinct (3) so "cannot ever revive" remains
  machine-detectable.
- **Gate the respawn on the specific condition (e.g. #927's component
  gating).** Condition-specific gates fix one incident each and multiply
  policy sites; the breaker is generic and catches the whole class.

## Consequences

- The watchdog's per-check failure line (`exit 1`) no longer appears for
  keepalive healthchecks; the failure signal is the WARNING lines and the
  events-stream alert. `EXIT_RESPAWN_FAILED` remains the browser
  healthcheck's own exit code (it does not use `run_keepalive`).
- Each watchdog process holds its own state: a watchdog restart forgets the
  breaker and re-alerts. Acceptable — same design as the pre-existing
  consecutive-failure counter, and the events stream is the durable record.
- Two watchdog daemons on one host (gateway + agent-runner) each alert for
  their own labels; per-process incident reporting is intended.
