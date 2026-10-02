"""Hosted runtime ownership: who owns an agent's runtime, and the fences that
enforce it on the durable inbound queue.

Package door — no imports, no re-exports; callers import the public modules:

  - `hosted.py`           — hosted incarnation admission, lease renewal,
    settlement and release (`admit_hosted_runtime`, `settle_hosted_runtime`,
    `apply_hosted_lifecycle`, `settle_stale_running_rows`); decides who owns
    the runtime.
  - `corpse_reap.py`      — terminate crash-dead rows the corpse mark names and
    commit each death's recovery wake (split out of `hosted.py` at its line
    budget).
  - `inbound.py`          — `lock_inbound_owner`: lock `agents_meta` before
    inbound rows and refuse a caller whose admitted incarnation no longer holds
    a fresh lease (`RuntimeOwnershipLostError`). Claim, reconcile, database
    recovery and settlement all mutate the queue under this lock.
  - `lifecycle_intent.py` — the one durable lifecycle command pointer: accept
    the oldest restart/terminate under the owner lock, settle a command whose
    target incarnation was replaced as superseded, and let the admitted
    successor observe a restart.

`hosted.py` decides ownership; `inbound.py` and `lifecycle_intent.py` enforce it
at each queue mutation. See `agent/ownership/docs/ownership.ava.okf.md`.
"""
