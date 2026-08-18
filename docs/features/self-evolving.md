# Self-Evolving

Ava's cluster upgrades itself. New code lands on `main`, and `ava cluster update`
rolls the whole cluster onto it — without stopping the work in flight.

## Why it matters

- **Hot updates** — an agent's current code execution finishes at its turn
  boundary before the new version takes over; only wedged processes are
  force-reaped. In-flight work is never interrupted.
- **Runs forever** — no maintenance windows, no "stop the world" releases.
  The cluster works by day and updates itself by night.
- **No babysitting** — the rollout is self-supervised: a canary runs the new
  code under observation while a holdout on the old code watches; on
  regression the holdout rolls back. No human in the loop.

## How it works

```
Phase A: quiesce every live agent (finish current turn, or reap if wedged)
   → migrate schema → pull new code
Phase B: bring every runner back on the new commit, canary first, holdout last
```

<!-- TODO(image): rollout sequence diagram — Phase A quiesce → migrate → Phase B resume -->

The release agent waits on the old code as the rollback executor (a timeout
escalates to a human), then switches itself over once the canary is healthy.

## Design decisions

- [Self-rolling release via canary-and-holdout](../../decisions/2026-05-09-self-rolling-release.md)
- [The CLI is the only update entry point](../../decisions/2026-08-05-cli-only-updates.md)
