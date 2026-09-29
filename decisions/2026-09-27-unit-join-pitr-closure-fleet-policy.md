# Unit join, PITR writer closure, and fleet policy defaults

## Context

Three questions came out of the FC-5, FC-6 and FC-8 slices of the unified
cluster lifecycle:

- FC-5 found that a join exchange authenticated by the human cluster secret
  would hand database credentials to anyone holding that secret, reopening what
  dbgen-6 closed. This conflicts with the letter of plan §2.12 and of
  `2026-09-27-write-generation-rollout-choices.md` item 4 ("manual per-unit
  credential bundles only for the one-time production cutover and emergencies").
- FC-6 made the release stop phase close persistent terminals and schedules,
  but PITR activation still refuses live terminals. Production always has them,
  so PITR could never activate there.
- FC-8's fleet workload policy needed semantic choices the plan left open.

## Decision

User ruling, 2026-09-27, accepting each recommendation:

1. **A new agent-runner joins with an operator-issued unit bundle.** The gateway
   operator runs `ava cluster db-authority issue-unit` for the new machine and
   home and supplies the sealed bundle at its first start (`--db-capability`).
   There is no join exchange authenticated by the human secret. The "manual
   bundles only for cutover and emergencies" rule governs *rollouts*: per-release
   credential exchange stays automated over the enrolled channel (dbgen-8).
2. **PITR activation closes terminals and schedules like a release.** It uses
   FC-6's closure: a bounded wait for work to finish, then a system-reason close,
   with each busy session's owner receiving the durable closure notice.
3. **Fleet workload policy defaults are accepted as recorded** in
   `cli/release_fleet/release_fleet.ava.okf.md`: a 30-minute watch window; recover
   when affected agents are strictly above 20 % of the cohort and at least two;
   one failed shared-core sample recovers immediately (no debounce); any affected
   agent marks the release degraded, never known-good; an empty cohort is not
   known-good; idle (parked) agents are outside the cohort; missing signals are
   unknown, never OK; the webhook is optional and named through a secrets file.

## Alternatives rejected

- **Bearer-authenticated join.** Any holder of the human secret could mint
  itself database access.
- **Keep refusing PITR on live terminals.** PITR activation would be impossible
  in production without manual cleanup of every terminal.
- **Debounced or looser fleet thresholds.** They trade faster commits for
  letting a failing release run longer; the defaults can be relaxed with
  production evidence.

## Consequences

- Adding a machine needs one extra operator step (issue and hand over a bundle).
- A PITR activation interrupts terminal sessions exactly like a release does.
- The first fleet releases may recover or mark degraded more often than
  necessary; the thresholds are `FleetPolicy` fields for later tuning.
