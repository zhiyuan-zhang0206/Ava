# Process-local SDK context with explicit host dependencies

Date: 2026-10-09
Status: Accepted
Supersedes: the SDK ContextVar and thread inheritance mechanism in
[the state/context decision](2026-10-04-ava-state-and-context.md).

## Context

Each `execute_code` call already runs in its own disposable Python process.
The host serves several agents concurrently through LangGraph
`Runtime[AvaContext]`. Keeping another current-context mechanism in SDK modules,
binding it around host turns, and patching `threading.Thread.start` duplicated
ownership that these boundaries already provide.

Ava is a personal agent sharing the user's local machine. This decision does not
introduce a security broker, independent execution service, daemon, RPC SDK,
custom import hook or SDK-instance framework.

## Decision

The execution bootstrap rebuilds `AvaContext` from the existing request
`describe()` data and installs it in the child-local `ava.context` module slot.
It does this before loading plugin surfaces and running user code. Context
contains process-local dependencies and identity; the request does not copy a
parent LangGraph Runtime, credentials or live clients. Clients remain lazy and
are closed by their owning process.

The existing `ava.state` working copy and `ava.state_update` delta remain the
state bridge. State stays lazily materialized under its existing policy. The
host owns durable state and applies the returned delta through existing
reducers. A legal delta written before a crash, cancellation or timeout still
returns; an invalid `state_update` is reported under the existing error contract.
No thread-safety or locking policy for mutable state is added here.

Normal `import ava`, `from ava import context`, plugin functions and threads
inside one execution use the same binding. SDK identity and connection readers
use this entry. SDK context does not use a ContextVar or modify thread startup.
The bootstrap runs separately from the original compiled user source, keeping
its source text and traceback line numbers intact.

The shared host never binds its current agent to the `ava` module. Graph nodes
use their Runtime, and helpers and plugin callbacks receive explicit context
or narrower arguments. Context-note builders receive `AvaContext`. Host memory
search and ancestor lookup pass the same context through the existing gateway
transport and retry policy. Native turn metadata remains for ownership and
observability; it never selects the SDK's identity.

Existing separate-process entry points retain their behavior: launched scripts
initialize from their explicit `AVA_AGENT_ID` environment channel, and a
schedule runner initializes its schedule actor. An external controller keeps
`with ava.external.attach(...): ava.*`: one exclusive attachment per process,
lease validation on every use, prior-context restoration and existing client
ownership on detach. Native turns and execution children still reject attach.
Arbitrary fork inheritance or multiple concurrent external identities in one
process are not promised.

## Consequences and verification

Two agents can execute concurrently in separate children while the host's SDK
binding stays unchanged. Child tests cover ordinary imports, thread calls,
context/state isolation, lazy client release, original traceback lines and
legal deltas from all existing result kinds. Parent-process tests retain real
signal timeout/cancellation and child reaping behavior. External tests retain
lease expiry, close failures and client ownership. Fleet receipt tests retain
independent SQL connections, concurrent admission and lease validation after a
real transaction-lock wait through explicit context business entries.
