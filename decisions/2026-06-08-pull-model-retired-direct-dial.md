# Pull model retired: gateway dials agent-runners directly

## Context

Gateway → agent-runner RPC (spawn, lifecycle, status probe, config, inventory,
cluster stop/update/resume) ran through a DB-backed queue. The gateway INSERTed a
`cluster_work` row and blocked on a notify channel; each agent-runner's daemon held
an outbound SSE stream to the gateway, claimed rows with `FOR UPDATE SKIP LOCKED`,
ran them in-process, and POSTed the result back, which fired the notify that woke
the blocked handler. The queue table, two notify channels, the SSE heartbeat, and
the daemon's reconnect/backoff loop all existed for one reason: a single
outbound-only node behind a TLS-MITM proxy that the gateway could not dial in to. It
was a pull model because the agent-runner could only reach out.

That constraint disappeared. The outbound-only node was decommissioned into its own
isolated cluster, the gateway joined the private network, and the project adopted the
invariant that **every node in a cluster is mutually reachable**. Once the gateway
can dial an agent-runner, the queue is pure overhead — a hop, two notify channels,
and a reconnect state machine standing in for one HTTP request.

## Decision

Each agent-runner runs an inbound ops server: it binds `0.0.0.0:<ops_port>` and
serves a single `POST /ops {kind, payload}` that dispatches the operation in-process
and returns `{status, result}` in the response. At startup the runner registers its
reachable ops URL in the `machines` table. The gateway dials it directly — one
synchronous HTTP round-trip, reading the address from the registry; an unreachable or
failed dial raises immediately and the caller retries.

Deleted: the `cluster_work` queue table, both notify channels, the SSE work-stream
and work-result endpoints, and all the claim / reset-stale-claims / reconnect /
backoff / heartbeat machinery. The "gateway URL is null for agent-runners" workaround
and the constraint that forced everything through the queue go with them.

## Alternatives rejected

- **Keep the pull model (queue + SSE).** The whole apparatus existed solely to serve
  one unreachable node. With mutual reachability assumed, it buys nothing and costs a
  queue hop, two notify channels, and a reconnect/heartbeat state machine. Pure
  overhead once the gateway can dial.

- **Keep the queue as a durable at-least-once fallback when a dial fails.** Rejected:
  two parallel transports defeat the simplification that motivated the change. The
  operations here — spawn, lifecycle, config — are synchronous anyway; a failed dial
  failing fast is better feedback than a row sitting `pending`. Full delete, pure
  direct-dial.

- **Transitional dual-transport release to ease the upgrade.** Rejected in favor of
  one clean cut. Changing the transport is inherently a bootstrap discontinuity that a
  compatibility window only prolongs (see Consequences).

## Consequences

- **One synchronous request/response replaces the queue.** Simpler error paths: a
  reachability failure and an operation failure are distinct, surfaced immediately at
  the call site, instead of a timeout on a blocked notify wait.

- **Direct-dial depends on the mutual-reachability invariant.** It is correct only
  while every node in a cluster can reach every other. A node that cannot be made
  reachable does not get a workaround back — it becomes its own isolated cluster.
  Cross-cluster communication is a separate, lower-frequency concern on its own bus,
  out of scope here.

- **No durable buffer for in-flight ops.** If the gateway cannot reach a runner, the
  request fails and the caller retries; nothing is queued on its behalf. Accepted,
  because these operations are synchronous and short-lived.

- **Changing the transport is a one-time bootstrap discontinuity.** A new-code gateway
  cannot reach an old-code runner (no ops server, no registered URL), and an old-code
  runner cannot pull (its queue table and work-stream are gone). The normal rollout
  fan-out — which itself rides this transport — cannot carry runners across this one
  change; each must be upgraded by hand once. An expand-contract migration does not
  help, since the discontinuity is the transport, not the schema. After every node is
  on the new code, fan-out works normally.
