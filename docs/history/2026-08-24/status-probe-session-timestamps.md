# Status probe session timestamps

## Decision

Keep status collection on the `SessionBackend.session_started_ats` bulk
boundary. The POSIX PTY backend resolves that boundary with one in-process
record scan; if the scan fails with an I/O error, it falls back to individual
record reads so the degraded path is no worse than the pre-batch behavior.

The PTY runtime has one detached host per session and no supervisor daemon, so
the earlier proposal for a supervisor-wide RPC no longer matches the runtime
boundary. The existing `list-started-at` CLI contract remains available for
external callers and is backed by the same record listing.

## Why

A status snapshot asks for every session timestamp. Paying one process or RPC
round trip per session amplified roughly 150 ms of fixed overhead into 4–5
seconds on a 28-shell host, exceeding the roster probe budget and making a
healthy machine appear offline. A single record scan makes cost depend on the
record population rather than interpreter startups or transport round trips.

## Rejected alternatives

- Increasing the status timeout alone preserves the latency amplification and
  only moves the false-offline threshold.
- Reintroducing a shared PTY supervisor solely for timestamp batching would
  weaken the per-session failure boundary and duplicate the record index that
  already exists.
