---
type: doc
title: Agent Lifecycle
description: Durable native controls executed by the agent host.
tags: []
---

# Agent Lifecycle

Lifecycle authority is a durable inbound and the admitted runtime incarnation.
`ops/ops_lifecycle.py` accepts restart/terminate/cancel through the home runner;
`agent/graph/_claim.py` applies their routing at the next claim boundary.

Normal restart/terminate returns from the graph, flushes the final checkpoint,
and applies the matching command through `agent/hosted_ownership.py`. The host
retains single-flight through settlement. Restart keeps the agent ID and allows
new admission; terminate leaves the ID, context and pending work available for
explicit resurrection. No per-agent exit callback or process restarter exists.

Force requests interrupt the exact hosted turn and its owned execution resources.
An enqueued force request is not proof of completed cleanup. An uncooperative
host task remains observable; stopping its whole host affects that runner's
other turns as well.

Persistent PTY shells are separate resources: native agent restart preserves
them. Cluster `pause` preserves them; full cluster `stop` closes them while
retaining durable agent data. Impersonation is an independent identity protocol.

## Related contracts

- [[process-lifecycle/reentry-paths.ava.okf.md]] — restart and resurrection
- [[startup/admission.ava.okf.md]] — ownership fencing
- [[sessions.ava.okf.md]] — persistent shell resources
- [[shared/maintenance.ava.okf.md]] — cluster pause/stop
