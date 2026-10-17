---
type: doc
title: Native process primitives
description: Native process identity, observation, signaling and root control.
tags: [base]
---

# Native process primitives

`base/native_process/` owns native process identity, observation, signaling and root control.
Its component nodes describe the current contracts and implementation.

`runtime_incarnation.RuntimeIncarnation` is the immutable original admission.
The host retains it after admission commits and hands the same object to
`MeivaContext.original_incarnation`; context copies preserve that reference.
The disposable child receives it in its request envelope and binds it through
the existing SDK process-context entry. A successor read cannot replace it.

`turn_identity.HostedTurnResources` holds the unresolved domains and late-reader
completions of one actual hosted turn Task. The host creates the scope before
starting the Task and passes it through `MeivaContext.hosted_resources` to exec
and settlement. Exact request/domain identity is required to discharge an entry.
`HostedServiceResources` is the explicit host lifespan that owns the real turn
roots, interrupt watchers and late completion tasks across turns. A late unknown
is reported immediately with its original scope, retained, and raised at service
stop/join without cancelling another agent. The existing cancellation-diagnostic
budget bounds join by an absolute deadline. Unfinished tasks keep their actual
handles, clients and pools until the daemon's existing hard exit; expiry is not
resource settlement. Successful join precedes client and database pool closure.
Hosted work, invocation and settlement register their original child Tasks in
this same service span. Registration keeps each result and exception intact;
it neither wraps the Task nor makes a failed invocation quiescent. A retained
turn may register its necessary settlement after service stop starts, and the
same remaining join deadline still applies.
Direct graph embedders with a database interrupt pool must create this service,
obtain a turn scope, pass it as `MeivaContext.hosted_resources`, and join the service
before closing their clients. `pool=None` subscriptions need no watcher owner.
Neither primitive exposes an ambient current getter or uses a ContextVar.
Graph identity comes from `Runtime[MeivaContext]` and SDK identity from the
execution process entry. Native ownership and resource-settlement fences remain
distinct from ordinary log attribution.

## Platform facts

`os_platform.is_linux()` and `os_platform.is_macos()`
read Python's `sys.platform` directly without retaining process-level copies.
Platform-dependent socket directory and length choices are made when the socket
is requested, rather than cached when its owner module imports.
Disk sampling uses the macOS data volume on macOS and `/` on other POSIX hosts;
no `uname` probe runs when the module imports.
Loaded commit and code version are separate boot facts and retain their frozen
process-generation contract.

## Signalling through a port

`process_control.ProcessControl` is the port for `kill`, `killpg`, `getpgid` and `getsid`,
the calls whose failure a test must be able to cause. A component that signals takes it as a
required argument; a composition root passes `SYSTEM_PROCESS_CONTROL`.
`ExecProcessDomain.launch_posix` takes the port with the launch and keeps that same one for
every later group signal and member-group read.
`tests/fake_process_control.FakeProcessControl` records the signals in
order and sends none, refuses chosen targets, answers group and session reads from a table, and can
forward to the system implementation for a test that signals a real child. Observing who is in a
group stays with `group_closure` and `ownership`.

## Documented components

- [[base/native_process/docs/group-closure.meiva.okf.md]] — Process-group closure core.
- [[base/native_process/root_control/docs/root_control.meiva.okf.md]] — Root control contract — protocol and Unix-socket client.

## Captured code as an entry-owned value

`loaded_commit.LoadedCommit.capture()` returns the source image captured at an
explicit process entry. Its `sha` is also the health response's fact: reading it
never invokes Git. `code_version.CodeVersion` retains that image and lazily
counts its first-parent commits, even if the checkout moves before the first
version read. An unknown capture stays unknown and raises `CodeVersionError`
when a gated database needs a version; a later checkout cannot stand in for the
image the process loaded.

## Clock-epoch test construction

`tests/test_proc_tree.py::test_capture_and_verify_span_clock_epochs` leaves one
epoch unpatched. The locked psutil macOS correction adds absolute boot-time
drift for either sign, so symmetric positive/negative patches would model the
same correction magnitude rather than two distinct epochs.
