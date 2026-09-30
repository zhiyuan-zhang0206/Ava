---
type: doc
title: Quiesced window — loops hold off, pools release
description: Between drain and resume, local background loops stop touching the database and every idle client-pool connection is released before the data plane closes.
status: current
---

# Quiesced window — loops hold off, pools release

`admission.quiesced()` (phases `drained` through `ready`) is the stop window
read by local background loops: the host daemon holds off ownership renewal and
page reconciliation, and the ops daemon's shell-closure-notice flush waits for
the hold to release.
None of them may borrow the database across the window; an unreadable owner
reads as quiesced, the same refuse-new-work posture the journal itself enforces
by raising. The host's pending-turn scan reads the narrower
`maintenance.in_stop_leg()` (`drained` .. `stopped`): it holds through the stop
leg, but from the start leg on a booting host must drain its pending workset
even while the unit is still held — pub/sub has no replay, so recovery may
not wait for the hold to release.

`ops.cluster_pause.release_local_db_pools` releases every idle client-pool
connection: the host daemon's shared and control pools (via `POST
/release-db-pools` on its loopback health port) and the ops daemon's own
dispatch pool, which the calling daemon passes in (ops never reaches into the
daemon's module state). `base.db.pool_release` performs the release against the pool's
private face because psycopg-pool has no public "close idle, keep usable"
operation (`drain()` re-opens replacements, `close()` is terminal). Its only
caller was the legacy `cluster_stop` op, which the scripted fleet update
replaced; it has no production caller now (recorded debt). Both host pools and
the ops pool run `min_size=0`, so the first borrow after resume reconnects
lazily.

## Dependencies

- [[maintenance.ava.okf.md|Native pause and maintenance]] — the hold, its
  phases, and the admission gates.
