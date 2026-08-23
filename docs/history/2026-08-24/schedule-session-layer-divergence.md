# Schedule session layer divergence

## Context

Schedule reconciliation on `ava-preview` repeatedly replaced `ava-schedule-*`
sessions while older PTY hosts and runners remained alive. The host process is
intentionally detached and ignores `SIGHUP` and `SIGTERM`, so a registry entry
is its normal management route. Logs showed a later host with the same name
starting before the earlier host had performed its normal shutdown.

## Decision

Treat only a precisely identified duplicate or recordless PTY host as a
recoverable orphan. The PTY CLI now matches the detached host's module
invocation, session name, and resolved record path. It retains the current
record owner, force-kills only extra hosts, and captures their descendants
before killing them so independently grouped schedule runners cannot escape.
`new` waits briefly for a possible host startup before reaping an unresponsive
recordless host; an explicit `kill` can force the same recovery immediately.

The host's `SIG_IGN` policy remains unchanged. Making ordinary `SIGTERM` end a
host would let stray signals and shell-tree termination break the persistent
session guarantee for every PTY client.

## Diagnosis boundary

The current source contains no PTY supervisor client or cached supervisor
connection: enumeration reads validated session records in-process. The live
preview checkout matches this implementation. Therefore the reported stale
supervisor-connection mechanism is not supported by the current code. The
exact reason that the preview gateway failed to see records cannot be proven
after the stopgap cleared the live broken state. Schedule reconciliation now
logs its backend type, resolved home, environment home, record directory, and
enumerated names at DEBUG level for a future occurrence.

The Phase-B update acknowledgement without a subsequent runner report uses a
different native-session/update path. No causal mechanism was proven here, so
its shutdown behavior was not changed.
