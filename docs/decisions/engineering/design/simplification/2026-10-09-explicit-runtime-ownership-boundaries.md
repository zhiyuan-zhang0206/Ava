# Explicit runtime ownership boundaries

Date: 2026-10-09
Status: Accepted

The user approved all four recommended choices below while retiring ambient-state
baselines. Acceptance records the intended contracts, not completion of their
implementation, validation or integration. Existing work is retired only after
its consumers migrate and a scan of merged code proves the sites are gone.

## Ordinary log attribution

Business audit, cancellation and lifecycle operations require explicit identity;
a missing required owner fails at the operation boundary. Ordinary unannotated
`logger.info(...)` and `telemetry.emit(...)` records may belong to the process or
system. They retain machine, process and actual loaded-generation evidence, but
need not appear in an agent-filtered view. Callers that need agent attribution
supply it explicitly. A ContextVar or a replacement ambient getter must not
infer a shared host's current agent.

The alternative was to carry an attributed logger or emitter through every turn
helper and preserve agent attribution for all ordinary logs. That wider API
change was rejected; explicit ownership of critical operations remains required.
Process startup facts must remain frozen to the interpreter's loaded generation.

## SDK entry counting

Each actual entry into a public SDK function is counted independently, including
internal fan-out into another public SDK function. Multiple plugin wrappers of
the same function install one recorder. Execution ownership may aggregate calls
from threads. Internal business code should use explicit-context business
functions where available.

This replaces the outermost-only count: a shell helper entering another public
SDK function can now increase both the count and the ranking. Sampling still
affects events rather than tally. Each call retains its admitted identity,
policy snapshot and original attachment receipt/gate; detach blocks new calls,
drains admitted calls, then seals. Counting never authorizes retrying effects
that already occurred or redirecting an old call into a new attachment.

The rejected alternative preserved exact outermost counting by carrying a
parent call scope across SDK and plugin boundaries. Its extra call contract
was not needed for the selected product behavior.

## Postcommit realtime notification

The current inbound request completes its realtime notification after the
message commits. An unknown notification error is exposed by that request; it
does not terminate the whole Gateway. The request therefore includes publish
latency. A durable receipt and stable logical key preserve whether the message
was already committed even when the response reports notification failure.
Wake, resurrection and realtime UI notification remain distinct operations.
Completion digests propagate through their own invocation or service boundary.

The rejected alternative kept notification asynchronous and connected its
unknown errors to the Gateway's actual service-stop boundary. A lifespan
TaskGroup alone does not guarantee uvicorn worker termination, and observing
an exception without exposing failure is not recovery. Expected transport
recovery remains explicitly typed and bounded; no general reliable-delivery
framework was approved.

## Heartbeat failure timing

Host and CLI relay heartbeat owners expose unknown errors when stopping and
joining the heartbeat, preserving the existing delayed timing. Host boot settle,
scheduler drain, ownership release and pool close retain their ordering and
finite-exit contract. A terminal lease result remains normal termination. The
CLI retains its existing error and exit-code contract; a TaskGroup migration
must not silently replace a single error with a swallowed ExceptionGroup.

The rejected alternative cancelled the owning host or relay immediately on an
unknown heartbeat error. That would change drain ownership and CLI propagation.
The selected timing is not a permanent lint exemption: explicit ownership must
eventually remove the sites while preserving bounded shutdown and delayed error
exposure. Deadlines do not prove that an underlying resource has stopped.

## Related choices

These choices follow the [dependency-injection direction](2026-10-02-dependency-injection-direction.md)
and [process-local SDK context](../../../agents/context/2026-10-09-process-local-sdk-context.md).
They do not change configuration refresh timing, original incarnation ownership,
resource settlement, existing admission fences or the separately paused skills
runtime cache. They do not authorize deployment or new baseline/allowlist entries.
