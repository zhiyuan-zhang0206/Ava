# Collector graceful exit fallback

The collector recovery path now has one bounded outcome for every verified
collector PID: request SIGTERM, wait at most five seconds, unconditionally
request SIGKILL, then verify that the PID exited. The non-session stale-listener
takeover returns `noop`, `graceful`, `forced`, or `survivor`; a survivor stays
in the listener probe and is logged rather than being reported as reclaimed.

The collector's normal session respawn now asks the native session backend for
the same five-second graceful window. Its existing backend ladder owns the
session tree and verifies its forced fallback, so the healthcheck does not try
to overlay a single-PID kill on a recorded session.

Five seconds leaves the collector's shutdown path an opportunity to finish but
keeps a wedged process to one grace window per 60-second watchdog round. The
remaining 20-second readiness verification still fits within that round.

Immediate SIGKILL was rejected because it bypasses the collector's shutdown
handlers; an unbounded graceful wait was rejected because it reproduces the
watchdog wedge. Treating a delivered SIGKILL as proof of death was rejected
because an uninterruptible or unsignalable process can still hold the listener.
