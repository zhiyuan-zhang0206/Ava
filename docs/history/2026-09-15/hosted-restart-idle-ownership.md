# Recover quiet ownership after host restart

We kept idle ownership recovery in the durable wake scan and normal admission
path. A separate metadata repair would duplicate the resource, lifecycle,
publication and maintenance fences, and could make maintenance appear ready
without a replacement runtime ever accepting responsibility. The scan discovers
expired or released hosted owners even without inbound work; admission and the
checkpoint's claim state decide what can actually resume.

We retained plugin restarts through the existing process signal. Cancelling
only the serving task would leave shielded inner agent turns alive after the
scheduler's bounded close. Instead, runtime cleanup joins every background task
and attempts drain, heartbeat stop and ownership release through an exit stack,
retaining failures after the remaining cleanup has run. Already-cancelling
background tasks are joined without a second cancellation that could interrupt
their own asynchronous cleanup.

The recovery scan excludes unowned intent and crash-marked rows. It does not
invent an inbound message, force a checkpoint to halt, or authorize takeover of
live managed resources. A halted empty-inbox checkpoint returns idle without
calling a model; interrupted autonomous work retains its existing semantics.

This code does not repair the production rows until an authorized rollout and
verified replacement host adopt it. The orchestrator running that first rollout
still uses its installed admission and maintenance behavior.
