---
type: doc
title: A continuation failure needs an owed continuation
description: The agent host reports a failed wake only while the agent still owes a continuation to a draining hold; another machine's woken agent, or a drained or parked member, is never reported.
status: current
---

# A continuation failure needs an owed continuation

Preparation captures the cohort under row locks: every non-terminated agent of
the machine, each as a restart command or parked. The host
(`services/agent_runner/agent_host/lifecycle/maintenance.py`) reports a failed
turn (`agent_continuation_lost`, ERROR) only for an agent that still has a
pending restart command in a draining hold (`admission.pending_command`).
The first report ends the agent's unapplied restart command (see the
[[maintenance.meiva.okf.md|maintenance doc]]); nothing is recorded in the journal
and no fence is set.

An agent with no pending command owes no continuation, so its failed wake is
not a continuation failure and is not reported:

- the agent is outside the captured cohort: another machine's agent, or one this
  hold never drains;
- the agent already drained, or is parked, or the hold is past `draining`: the
  hold reached `drained`, which required every member drained, or any later phase.

The wake of a drained or parked member reads its row and returns: no restart
command is pending, so the held control that claims lifecycle commands never
runs, and ordinary inbound messages stay pending. The resume wakes drained
members; the host's pending-inbound scan picks up parked ones once the hold is
released. These facts come from the local journal, so the rule holds when the
row read never ran. That is the case it exists for (FC-10 F20): every runner
receives every wake (the dispatcher's subscription is cluster-wide), and a
stop's shutdown cancels all tasks before `_read_stored_config` can say "not
ours".

## Dependencies

- [[maintenance.meiva.okf.md|Native pause and maintenance]] — the hold, its
  phases and the drain.
