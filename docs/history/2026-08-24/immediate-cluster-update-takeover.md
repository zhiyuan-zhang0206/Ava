# Immediate cluster-update takeover

## Context

The cluster-status poll could discover a rollout only after its next interval,
while an authenticated browser retained its session state when the gateway went
down. An already-open tab could therefore keep accepting interaction during the
restart window and show the updating page only after a manual refresh.

The last successful status response also remained in TanStack Query. Setting
only the Zustand updating flag from a live event allowed that pre-update
snapshot to mirror `false` back into the flag before the gateway stopped.

## Decision

Successfully spawning a whole-cluster rollout or restart publishes the global
`cluster_update_started` live-projection event. It uses `agent_id=0` because the
event has no owning agent, and carries the orchestration kind and trigger origin.
Single-host update relays and watchdog self-heal paths do not publish it because
they do not interrupt the gateway serving the browser.

The frontend treats the event as a render hint: it seeds the cluster-status
cache with the started orchestration and then enables the full-screen updating
takeover for every auth status. Completion remains owned by the existing status
poll, whose finish edge clears the takeover, reconnects SSE, and refetches agent
state.

## Rejected alternatives

- Retrying auth and reloading from the updating page can reload-loop while the
  gateway is still healthy between orchestration spawn and shutdown.
- A separate completion event would duplicate the established poll edge and is
  least reliable at the exact point where the gateway and SSE connection bounce.
- Setting only the Zustand flag leaves the stale status-cache race intact.
