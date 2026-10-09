# Retry consumer contract audit

The shared `Policy`, `retry` and `aretry` primitives live in
`base/host/net/resilience.py`; bootstrap fetches already use them. This item is
unimplemented verification work for the remaining Redis consumer, not a
new retry framework or a requirement to force every loop through one executor.

- `base/events/live/redis_listener.py` still recovers from `TypeError`. Audit that
  branch alongside its wait budget, cancellation and durable recheck semantics;
  do not remove those semantics just to share a retry loop.

Any implementation needs consumer-specific regression evidence for expected
transient recovery and unknown failures. This document changes no runtime
behavior.
