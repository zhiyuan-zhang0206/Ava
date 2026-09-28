"""Runtime ownership fences on the durable inbound queue.

Package door — no imports, no re-exports; callers import the public modules:

  - `inbound.py`          — `lock_inbound_owner`: lock `agents_meta` before
    inbound rows and refuse a caller whose admitted incarnation no longer holds
    a fresh lease (`RuntimeOwnershipLostError`). Claim, reconcile, database
    recovery and settlement all mutate the queue under this lock.
  - `lifecycle_intent.py` — the one durable lifecycle command pointer: accept
    the oldest restart/terminate under the owner lock, and settle a command
    whose target incarnation was replaced as superseded.

Hosted admission, lease renewal and settlement (`agent/hosted_ownership.py`)
decide who owns the runtime; this package enforces that decision at each queue
mutation. See `agent/lease.ava.okf.md`.
"""
