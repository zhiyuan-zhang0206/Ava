---
type: doc
title: Nothing failable ahead of the respawn
description: The probe contract generalized to the whole healthcheck main() — once the verdict is dead the respawn is the next thing that happens, so anything placed between them becomes a precondition for recovery. Covers the order rule and the bounds on work the daemon cannot do.
tags:
- ops
---

# Nothing failable ahead of the respawn

The probe is not the only place a healthcheck can lose its restart. The rule generalizes to the whole `main()`:

> Once the verdict is **dead**, the respawn is the next thing that happens. Anything placed between them becomes a precondition for recovery.

`restarter.py` broke this: between the dead verdict and `_restart_daemon()` sat a catch-up that opened a DB connection. A dead DB raised out of `main()`, the watchdog isolated it, the round survived — and the respawn was never attempted. Same signature as a raising probe, one frame out.

What makes it a design error rather than a missing `try` is the coupling: a DB outage and a daemon crash are independent events, so gating recovery from the second on the health of the first means one outage takes out the other's recovery path. The fix was to **delete** the catch-up, not wrap it — the revived daemon's own `RespawnController` sweeps the backlog on its first tick (~1s), machine-scoped and gateway-gated, which the hand-rolled copy was neither.

Two rules follow for any healthcheck that grows a second responsibility:

- **Order**: probe → respawn → verify → report. Reconciliation belongs to the daemon being revived, not to the thing reviving it. The one verdict that skips the respawn instead of preceding it is the terminal one, where no respawn could succeed ([[services/healthchecks/terminal-verdict/terminal-verdict.ava.okf.md]]).
- **Work the daemon cannot do** (`restarter.py:_standin_dispatch`, for the rounds that will have no live daemon at all) runs **after** the respawn verdict — or in place of a respawn that would be futile, never ahead of one — is total (never raises, so it cannot mask the non-zero exit), and is **bounded**: it executes inside the watchdog's sequential tick, so a long wait there delays every check behind it.

`shared.service_respawn.run_keepalive` holds both rules for every daemon healthcheck, so they are properties of the shared runner rather than of each module keeping its `main()` in the right shape.

## Key Dependencies
- [[services/healthchecks/probe-contract/probe-contract.ava.okf.md]] — the probe-level rule this generalizes
- [[services/healthchecks/restarter-standin.ava.okf.md]] — the one healthcheck that has such work, and how it is bounded
- [[services/healthchecks/terminal-verdict/terminal-verdict.ava.okf.md]] — the verdict where the respawn is skipped rather than attempted
