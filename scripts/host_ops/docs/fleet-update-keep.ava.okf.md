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

```ava-contract
id = "component-keep-decision"
kind = "invariant"
statement = "A component is kept only when no changed file of OLD..NEW lies in the union of its dependency closures at both revisions; roots are the tracked files under its dirs with tests and docs excluded, watch entries are bare files, never expanded, and a file deleted in NEW still counts through the old revision's closure; OLD == NEW short-circuits to keep-everything — the rerun of an interrupted down."
code = ["scripts/host_ops/fleet_update_keep.py:Component", "scripts/host_ops/fleet_update_keep.py:COMPONENTS", "scripts/host_ops/fleet_update_keep.py:component_surfaces", "scripts/host_ops/fleet_update_keep.py:decide"]
tests = ["scripts/host_ops/tests/test_fleet_update_keep.py::test_no_change_keeps_everything", "scripts/host_ops/tests/test_fleet_update_keep.py::test_a_pty_change_restarts_only_pty", "scripts/host_ops/tests/test_fleet_update_keep.py::test_a_transitive_dependency_restarts_the_consumer", "scripts/host_ops/tests/test_fleet_update_keep.py::test_a_browser_dependency_restarts_the_browser", "scripts/host_ops/tests/test_fleet_update_keep.py::test_an_unrelated_change_keeps_both", "scripts/host_ops/tests/test_fleet_update_keep.py::test_a_deleted_surface_file_restarts_its_component", "scripts/host_ops/tests/test_fleet_update_keep.py::test_an_added_file_under_a_component_dir_restarts_it"]
```

```ava-contract
id = "component-roots-and-watch"
kind = "invariant"
statement = "Roots are the tracked files under the component dirs with tests and docs excluded; watch entries enter the surface as bare files; a surface node that is a directory covers everything below it; the closure follows dependency chains and survives cycles."
code = ["scripts/host_ops/fleet_update_keep.py:Surface", "scripts/host_ops/fleet_update_keep.py:roots_at", "scripts/host_ops/fleet_update_keep.py:closure", "scripts/host_ops/fleet_update_keep.py:hit_files"]
tests = ["scripts/host_ops/tests/test_fleet_update_keep.py::test_tests_and_docs_under_a_component_are_not_roots", "scripts/host_ops/tests/test_fleet_update_keep.py::test_a_watch_file_restarts_its_component", "scripts/host_ops/tests/test_fleet_update_keep.py::test_roots_at_skips_tests_and_docs_and_watch_files", "scripts/host_ops/tests/test_fleet_update_keep.py::test_the_closure_follows_chains_and_survives_cycles", "scripts/host_ops/tests/test_fleet_update_keep.py::test_a_surface_directory_covers_everything_below_it"]
```

```ava-contract
id = "keep-decision-fail-closed"
kind = "invariant"
statement = "In a judged window (OLD != NEW) every failure — an unresolvable revision, a git or worktree failure — prints one message on stderr and exits non-zero, and a component whose dirs or watch entries match no tracked file at either revision restarts; the caller must then restart everything rather than guess a keep."
code = ["scripts/host_ops/fleet_update_keep.py:main", "scripts/host_ops/fleet_update_keep.py:decide"]
tests = ["scripts/host_ops/tests/test_fleet_update_keep.py::test_a_component_with_no_matching_root_restarts_conservatively", "scripts/host_ops/tests/test_fleet_update_keep.py::test_a_watch_file_absent_at_both_revisions_restarts_conservatively", "scripts/host_ops/tests/test_fleet_update_keep.py::test_a_bad_revision_fails"]
```

```ava-contract
id = "scratch-worktree-cleanup"
kind = "invariant"
statement = "Both revisions are checked out into one temporary worktree parent, and the judgment dismantles them — worktrees removed, pruned, parent deleted — so no registered worktree survives."
code = ["scripts/host_ops/fleet_update_keep.py:decide", "scripts/host_ops/fleet_update_keep.py:_worktree", "scripts/host_ops/fleet_update_keep.py:_dismantle"]
tests = ["scripts/host_ops/tests/test_fleet_update_keep.py::test_a_pty_change_restarts_only_pty"]
```

Parent: [[scripts/docs/scripts.ava.okf.md]].
