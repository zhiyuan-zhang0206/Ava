---
type: doc
title: Retained updater handoff evidence
description: Retained exact-generation evidence that still fences admission and recovery.
status: current
---

# Retained updater handoff evidence

`shared/updater_handoff.py` retains the exact generation-guarded schemas and
readers for `$AVA_HOME/run/updater-handoff.json` and the bootstrap/normal recovery
envelope. Runtime admission and recovery refuse unresolved evidence. The old
CLI/ops producer graph, bootstrap-hop and continuation commands are absent.

Storage mutation APIs still require the updater OS mutex, exact generation and
native owner evidence. Atomic mode-0600 publication and generation CAS preserve
an interrupted owner's evidence. An expired deadline alone does not prove child
exit. A malformed or unfinished nested bootstrap/normal journal cannot be cleared
by generic recovery, and retained evidence is not automatic rollback permission.

The stable lock inode and pending-publication fences remain until their explicit
replacement. Old on-disk state requires the operator's one-time cutover; it is
not adopted into the prepared release operation journal.
