---
type: doc
title: Fleet update keep decision
description: Which stateful components a source-mode update window keeps, judged from the window's dependency closure at both revisions.
tags: [scripts, operations, updates]
---

# Fleet update keep decision

`scripts/host_ops/fleet_update_keep.py` answers one question for `down`:
which stateful service groups the window's changes do **not** force to
restart. Two hold state that a stop would destroy — the pty-sessions service
(every agent shell on the machine) and the browser pair (`browser` +
`browser-mcp`: the headed Chrome with its logins and the shared
chrome-devtools MCP it serves). The judgment is generated, never a hand-kept
list: per component the tool takes the component's directories as roots,
expands `meizel.contracts.analysis` facts of a revision into a file closure,
and reports `keep` when no changed file of `OLD..NEW` lies in the union of
the closures at both revisions. A file deleted in NEW is still in OLD's
closure, so a deletion counts. The few files a component owns by convention
rather than import (the machine-side probes) are watch entries: bare files,
never expanded. The `COMPONENTS` table is the single owner of the component
definitions.

`python scripts/host_ops/fleet_update_keep.py --repo R --old SHA --new SHA`
prints one line of JSON on stdout (`keep_services` plus a per-component
verdict); anything else prints to stderr and exits non-zero, and the caller
then restarts everything rather than guess. In a judged window, a component
whose dirs or watch entries match nothing at either revision also restarts:
the table is out of date. Run it with the checkout's own
interpreter: it imports `meizel.contracts.analysis` from that checkout, as
the locality gate does.

Measured on 2026-10-17 (`main@2c3b7c366e`): the pty surface is 31 files; the
browser surface is 152 — it reaches `base/config`, `base/events` and
`base/telemetry`, so browser keeps stay the minority until that surface
narrows.

The tool's tests run against miniature real checkouts: tiny git repositories
whose component files carry real imports, so a hit can only come from the
graph, exactly as on the full checkout.

## Contracts

A component is kept only when no changed file of OLD..NEW lies in the union of its dependency closures at both revisions; roots are the tracked files under its dirs with tests and docs excluded, watch entries are bare files, never expanded, and a file deleted in NEW still counts through the old revision's closure; OLD == NEW short-circuits to keep-everything — the rerun of an interrupted down.

Roots are the tracked files under the component dirs with tests and docs excluded; watch entries enter the surface as bare files; a surface node that is a directory covers everything below it; the closure follows dependency chains and survives cycles.

In a judged window (OLD != NEW) every failure — an unresolvable revision, a git or worktree failure — prints one message on stderr and exits non-zero, and a component whose dirs or watch entries match no tracked file at either revision restarts; the caller must then restart everything rather than guess a keep.

Both revisions are checked out into one temporary worktree parent, and the judgment dismantles them — worktrees removed, pruned, parent deleted — so no registered worktree survives.

Parent: [[scripts/docs/scripts.meiva.okf.md]].
