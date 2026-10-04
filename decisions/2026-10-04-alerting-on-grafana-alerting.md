# Alert notification lives in Grafana Alerting; the code only emits signals

## Context

Alerts reached the user through two layers. Grafana evaluated rules and posted a webhook to the
gateway ingest, which stored the row and sent the IM. Beside it, a dozen processes wrote their own
alert rows or pushed IM directly (the health probe with a local-ingest fallback, the machine and
station liveness passes, the exec child, the inspector, the impersonation event log, the root unit
episode store, the IM push watchdog), and a second set of code decided when a condition counted:
consecutive-round thresholds, a 180s/600s transition clock, deploy-window holds with a settle
grace, a per-host cooldown marker, a per-agent post limit. The 2026-10-04 noise batch (#4217) added
four more of the second kind. Two layers of de-duplication meant no single place answered "why did
this page, why did that not", and every new alert had to choose which layer to put its debounce in.

## Decision

1. One path: signal (a declared event or metric) -> Grafana rule, whose `for:` and trailing window are
   the debounce -> notification policy (`group_by [alertname]`, 30s wait, 5m interval, 4h repeat;
   critical skips the wait) -> the existing webhook -> the gateway's `POST /api/alerts` -> IM. The
   ingest is the only writer of the `alerts` table and the only IM alert fan-out; its
   (fingerprint, starts_at) idempotence stays because it is protocol idempotence, not noise control
   (`tests/scripts/test_alert_single_path.py` holds the single path).
2. Code decides only what a log line or event means at its source (level by cause, one summary per
   tree, a detection threshold such as "a borrow over 10s is slow") and emits it while a condition
   holds. Every in-code notification gate is deleted with its keys, tests and docs; each deleted
   self-made alert has a rule in `rules.yml` (table in the alerting README).
3. A planned outage is one Grafana silence over every rule, opened by `cli.fleet_update down` before
   the first stop and expired by `up`, with an expiry and a comment. The disk alerts
   (`metric=host_disk`, `attributes_check=disk_usage`) are exempt. The program ships on stdin so the
   update that introduces it already has it, and failing to open it warns instead of stranding the
   update.
4. The gateway's reconciliation of stored rows against Grafana's active alerts stays
   ([2026-08-23](2026-08-23-alert-ingest-reconciliation.md)): a lost resolved webhook is not a
   noise problem, it is stored state diverging from the evaluator, and Grafana's own API is the
   truth it reads. With one writer left it no longer filters by `source`, so a row a retired in-process
   writer left open closes with the rest. The policy's 4h repeat still supports that decision's reasoning.
5. Failure of the path itself is covered by three independent things, because Grafana is now the
   only route. The probe emits `health_probe_ran` every run and a rule fires when it goes absent
   (Grafana is alive, events or the probe are not). `gateway_liveness` and that dead-man rule also
   go to Grafana's native Telegram notifier, which does not depend on the gateway, with the token and
   chat id converge renders from the telegram settings. A whole-machine outage of the station, and so
   of Grafana, is the out-of-band probe's.

## Alternatives rejected

- **Keep the in-code gates and put Grafana only in front of them.** Two debounces compound and
  neither can be read from the other; the #4217 gates would each need a Grafana twin anyway.
- **Mute timings for the window.** They need a machine-readable source of the window; the update
  tool is that source, and a silence carries the expiry and the comment an operator reads.
- **Inhibition rules for warning-then-error.** Grafana's file provisioning has no inhibition surface.
  A 3-minute and a 10-minute rule coexist per condition; the machine-offline pair hands off through
  the `consecutive_failures` filter instead.
- **Rules over the observatory station's reachability.** Loki and Grafana live on the station and the
  events travel the path such a rule would watch, so it could only be blind exactly when it mattered.
- **Deleting the reconciliation.** Considered with the in-code noise machinery and kept: it repairs
  state, it does not throttle notifications.

## Consequences

- Grouping bounds webhook posts, not IM count: the ingest still sends one IM per alert instance.
- A long outage holds two instances (warning, error) per condition; a lost resolved webhook is
  repaired by reconciliation within five minutes.
- Recovery of a state alert is the trailing window after its last event (minutes), not the next
  healthy pass.
- Telegram-routed alerts reach the user twice when the gateway is up (webhook IM and the direct
  message); accepted for the two conditions that matter when it is down.
- The `alerts.source` column keeps its history values; only `grafana` is written now.
- A `machine_probe.transition_since` column lost its last writer and is dropped by migration.
