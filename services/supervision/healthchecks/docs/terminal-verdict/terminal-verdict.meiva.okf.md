---
type: doc
title: Readiness verdicts and recovery authority
description: Four evidence states determine whether root may consider replacement.
tags:
- ops
---

# Readiness verdicts and recovery authority

| Verdict | Evidence | Root action |
|---|---|---|
| ALIVE | Owned generation passed its real protocol | Clear the continuous-outage restart budget. |
| DOWN | Owned protocol failed, or known expected generation/listener is absent | Consider one process restart within intent, startup grace and finite attempt budget. |
| PORT_TAKEN | Another generation owns the responding endpoint | Report; do not kill or spawn into the occupied endpoint. |
| UNAVAILABLE | Inspection, identity, owner status, or probe execution is uncertain | Report; do not infer absence or permission to restart. |

Root's `HealthMonitor` allows three actual restart calls during a continuous
outage, one per ordinary round. Later rounds only observe and alert. There is
no replacement confirmation loop or adaptive backoff. A new ALIVE observation
or observed operator stop resets the count; terminal observations and maintenance
holds do not replenish it. A code-version-gate exit requires updating the host,
so root does not automatically restart that process.

Status and readiness use the same probe evidence. No pidfile, session record,
matching home, or matching executable can substitute for captured ownership.
Root retains known direct-child identities for bounded stop signals and reap;
this does not certify closure of all descendants. A probe worker deadline yields
unavailable and retains the worker to prevent concurrent attempts; late green
results are discarded. The next ordinary round observes a replacement's readiness.

[[services/supervision/meiva_root_glue/docs/meiva_root_glue.meiva.okf.md|Root wiring]] separates this policy
from read-only diagnostics and external transitions.
