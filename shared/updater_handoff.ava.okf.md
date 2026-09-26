---
type: doc
title: Retained updater handoff evidence
description: Retained exact-generation evidence that still fences admission and recovery.
status: current
---

# Retained updater handoff evidence

`shared/updater_handoff.py` retains the exact generation-guarded readers for
`$AVA_HOME/run/updater-handoff.json` and the bootstrap/normal recovery envelope
(schemas in `shared/updater_recovery.py`). No current code writes either file:
the old CLI/ops producer graph, its writers, bootstrap-hop and continuation
commands are absent. A host upgraded from the retired updater may still carry
them, so cluster resume and recovery (`ops/ops_cluster.py`) read them and refuse
unresolved evidence.

The only mutation left is `clear(generation)`: an exact-generation CAS under the
handoff lock, permitted only when the retained envelope is terminal. It removes
that generation's spawn-attempt evidence directory first, then the envelope and
the marker, so an interrupted clear replays to convergence. An expired deadline
alone does not prove child exit; a running owner reads as dead only on exact
PID + native birth evidence. A malformed or unfinished nested bootstrap/normal
journal cannot be cleared by generic recovery, and retained evidence is not
automatic rollback permission. An unreadable marker has no in-tree clear; the
operator removes it in the cutover record.

The stable lock inode and pending-publication fences remain until their explicit
replacement. Old on-disk state requires the operator's one-time cutover; it is
not adopted into the prepared release operation journal.
