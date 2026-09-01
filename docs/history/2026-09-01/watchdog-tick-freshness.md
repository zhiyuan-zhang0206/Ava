# Watchdog completed-tick freshness

## Decision

Each capability watchdog now owns a distinct per-unit `/healthz` listener and
records a completed-round timestamp only after its controller reconciliation and
healthcheck roster both return. The timestamp drives both the health response's
`last_tick_at` field and the `watchdog_tick` OTLP ObservableGauge.

A watchdog round has a 90-second deadline. A timeout logs an error, skips the
rest of that round, and does not advance either freshness signal. Controllers
may additionally declare a reconcile timeout; because their synchronous work
runs in an unkillable worker thread, an expired controller blocks the remainder
of the current round rather than allowing later controllers or service
healthchecks to race an unknown outcome.

The Prometheus alert identifies each recently seen `(machine, process)` whose
timestamp is older than three minutes or whose tick metric has disappeared.
The historical/current set subtraction retains an alert after Prometheus marks
a stopped series stale, but naturally retires a capability absent for 24 hours.

## Consequences

- A live watchdog process can no longer report perpetual health while its work
  is wedged.
- Gateway and agent-runner watchdogs cannot contend for one health port on a
  co-located unit; older records preserve their fixed legacy ports until they
  are reborn.
- Native Grafana CI provisions the complete shipped tree and checks that every
  alert rule appears through Grafana's provisioning API, making invalid rule
  configuration fail before deployment.
