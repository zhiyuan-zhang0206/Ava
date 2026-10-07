# Inbound wedge P0 recovery

## Decision

Idling agents use a separate 180-second pending-inbound threshold. Running
agents retain the long threshold derived from the exec and LLM retry budgets.
The process-mode wedged controller also reaps a terminated row that still holds
a live lease, pid, and pending terminate inbound, without resurrecting it.

The Redis wake-key GETDEL shares the caller's wait budget. A timeout closes the
command and pubsub clients before the caller performs its fallback database
recheck, so a cross-machine half-open Redis socket cannot hold the claim loop
forever.

## Rationale

An idling agent has no legitimate turn budget that permits a pending inbound to
remain unclaimed for the running-agent threshold. A terminated row instead
expresses user intent, so clearing its stale process projection must not launch
a replacement. The bounded GETDEL protects the immediate suspect, while the
out-of-process controller covers the broader stalled-pickup symptom.
