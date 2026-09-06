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

A service crash and a DB outage are independent events. A healthcheck must
not require a working DB before attempting to revive a dead service: that
couples recovery from one outage to the absence of another.

The order is probe → respawn → verify → report. Reconciliation belongs to
the daemon being revived. A terminal verdict skips respawn because another
unit owns the port; see
[[services/healthchecks/terminal-verdict/terminal-verdict.ava.okf.md]].

Any additional recovery work must run after the respawn verdict, be bounded,
and preserve its failure signal. Healthchecks run in the watchdog's sequential
tick, so a long wait also delays checks for unrelated services.

`shared.service_respawn.run_keepalive` holds both rules for every daemon healthcheck, so they are properties of the shared runner rather than of each module keeping its `main()` in the right shape.

## Key Dependencies
- [[services/healthchecks/probe-contract/probe-contract.ava.okf.md]] — the probe-level rule this generalizes
- [[services/healthchecks/terminal-verdict/terminal-verdict.ava.okf.md]] — the verdict where the respawn is skipped rather than attempted
