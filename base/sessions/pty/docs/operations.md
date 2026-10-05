# PTY allocation operations

The [generation boundary](generation-boundary.ava.okf.md) owns the freeze effect
on schedules, watcher exclusions and fail-closed marker repair. The
[session ledger](pty_sessions/closure-and-ledger.ava.okf.md) owns inventory and
identity-aware closure. A freeze is not an inspection-only pause: fence any
reconciler whose existing sessions must be retained before changing generation.

## Freeze and resume

```bash
ava pty freeze --holder idle-fix-operator --reason "manifest and bounded cleanup"
ava pty status
ava pty resume <generation-token>
```

Retain the random token printed by your freeze. Its acknowledgement is the
allocation boundary: earlier allocations have been recorded; later missing-name
allocations are refused. An already-live name remains idempotent. A stale token
cannot resume a replacement freeze. These local host commands remain available
while the gateway, Postgres or Redis is down.

A malformed marker fails closed. Inspect the marker path reported by `ava pty
status`, recover its generation from a known-live session and follow the repair
contract above. Do not delete the marker or infer a resume from missing evidence.
Coordinate selective cleanup through the [runbook sequence](../../../../docs/conventions/runbook.md#emergency-pty-allocation-freeze).
