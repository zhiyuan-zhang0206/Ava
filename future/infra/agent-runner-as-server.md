# Agent runner as a server

The agent host is the only agent execution architecture. One daemon per runner
schedules agent turns as asyncio tasks. The per-agent process launcher, process
restarter, boot-attempt recovery and runtime-mode selector are removed.

The implemented contracts live in [Agent Runtime](../../agent/agent-runtime.ava.okf.md),
[Hosted Runtime Admission](../../agent/startup/admission.ava.okf.md) and
[Graceful Maintenance](../../conventions/graceful-maintenance.md).

## Resource boundaries

The host shares its graph and database pools. Agent identity, framework config
and plugin config are context-bound; model/runtime caching is bounded by size
and idle lifetime. Idle ends a task without a model call. Redis wake events are
multiplexed and durable pending work supplies missed-wake recovery.

`execute_code` remains a disposable subprocess with an owned resource domain.
Persistent shells have independent PTY hosts and survive pause/update. Full
stop closes them deliberately. Neither is an alternate agent execution mode.

## Remaining design considerations

An asyncio task blocked in a non-cooperative native call cannot be killed
individually. Cancellation retains single-flight until the actual task unwinds;
the host reports an uncancellable turn. Killing its daemon affects every turn
on that runner. Any future worker sharding must preserve exact-incarnation
admission, checkpoint settlement and execution-resource ownership while reducing
that blast radius; it must not restore a parallel per-agent process protocol.
