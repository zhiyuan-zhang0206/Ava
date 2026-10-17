---
type: doc
title: Agent Lifecycle
description: Durable native controls executed by the agent host.
tags: []
---

# Agent Lifecycle

Lifecycle authority is a durable inbound and the admitted runtime incarnation.
`ops/lifecycle/__init__.py` accepts restart/terminate/cancel through the home runner;
`agent/graph/claim/node.py` applies their routing at the next claim boundary.

Normal restart/terminate returns from the graph, flushes the final checkpoint,
and applies the matching command through `agent/ownership/hosted.py`. The host
retains single-flight through settlement. Restart keeps the agent ID and allows
new admission; terminate leaves the ID, context and pending work available for
explicit resurrection. No per-agent exit callback or process restarter exists.

Force requests interrupt the exact hosted turn and its owned execution resources.
An enqueued force request is not proof of completed cleanup. An uncooperative
host task remains observable; stopping its whole host affects that runner's
other turns as well.

Persistent PTY shells are separate resources: native agent restart preserves
them. `meiva restart` and full cluster `stop` close them while retaining durable
agent data. Impersonation is an independent identity protocol.

## Related contracts

- [[process-lifecycle/reentry-paths.meiva.okf.md]] — restart and resurrection
- [[agent/startup/docs/admission.meiva.okf.md]] — ownership fencing
- [[agent/docs/sessions.meiva.okf.md]] — persistent shell resources
- [[base/deploy/maintenance/docs/maintenance.meiva.okf.md]] — cluster stop/restart
