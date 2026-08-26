# Self-Evolving

Ava's cluster upgrades itself. New code lands on `main`, and `ava cluster update`
rolls the whole cluster onto it — without stopping the work in flight.

## Why it matters

- **Hot updates** — every live agent is signalled to restart and exits at its
  turn boundary inside the quiesce window (default 10s,
  `AVA_UPDATE_QUIESCE_TIMEOUT_SECONDS`); anything still live at the deadline
  — a long `execute_code`, a wedged process — is force-reaped. Since the
  2026-08-26 ruling the window is deliberately short: an in-flight exec is
  usually cut short, accepted in exchange for a fast cluster unblock.
- **Runs forever** — no maintenance windows, no "stop the world" releases.
  The cluster works by day and updates itself by night.
- **No babysitting** — the rollout is self-supervised: a canary runs the new
  code under observation while a holdout on the old code watches; on
  regression the holdout rolls back. No human in the loop.

## How it works

```
Phase A: quiesce every live agent (exit at turn boundary within the short
         window, then reap whoever is still live)
   → migrate schema → pull new code
Phase B: bring every runner back on the new commit, canary first, holdout last
```

<!-- TODO(image): rollout sequence diagram — Phase A quiesce → migrate → Phase B resume -->

The release agent waits on the old code as the rollback executor (a timeout
escalates to a human), then switches itself over once the canary is healthy.

## Design decisions

- [Self-rolling release via canary-and-holdout](../../decisions/2026-05-09-self-rolling-release.md)
- [The CLI is the only update entry point](../../decisions/2026-08-05-cli-only-updates.md)
