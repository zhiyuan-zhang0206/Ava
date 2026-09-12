---
type: doc
title: Quiesced window — loops hold off, pools release
description: Between drain and resume, local background loops stop touching the database and every idle client-pool connection is released before the data plane closes.
status: current
---

# Quiesced window — loops hold off, pools release

`maintenance.quiesced()` (phases `drained` through `ready`) is the stop window
read by local background loops: the host daemon holds off ownership renewal,
its pending-turn scan and its page reconciliation, and the ops daemon defers
its shell-closure-notice flush. None of them may borrow the database across the
window; an unreadable owner reads as quiesced, the same refuse-new-work posture
the journal itself enforces by raising.

The ops `cluster_stop` step — after the agent drain, before the data plane
closes — releases every idle client-pool connection: the host daemon's shared
and control pools (via `POST /release-db-pools` on its loopback health port)
and the ops daemon's own dispatch pool. `shared.pool_release` performs the
release against the pool's private face because psycopg-pool has no public
"close idle, keep usable" operation (`drain()` re-opens replacements,
`close()` is terminal). Both host pools and the ops pool run `min_size=0`, so
the first borrow after resume reconnects lazily.

## Dependencies

- [[maintenance.ava.okf.md|Native pause and maintenance]] — the hold, its
  phases, and the admission gates.
