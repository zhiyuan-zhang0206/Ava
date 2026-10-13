---
type: doc
title: "meizel/ — Narrowed Endpoints"
description: "A declared edge may name a deeper path on either side, so a directory can say that only part of a child may use part of another; coverage, staleness, cycles and sync --declare."
tags:
  - locality
  - tool
---

# meizel/ — Narrowed Endpoints

## What is it

An edge in a directory's `[uses]` normally joins two direct children. Either
side may instead name any tracked path below the directory, written relative to
it. The root manifest can say that one service, not all of them, may use part of
the kernel:

```toml
[uses]
"services/agent_runner/agent_host" = ["agent/process_boot.py", "agent/graph"]
```

A plain child name is the same declaration with the child itself as the path, so
existing manifests mean what they always meant.

## Semantics

- **Coverage.** A fact is covered when its source lies under a declared source
  path and its target under the declared target path of the same pair. A file is
  a path under itself. `services/other/x.py -> agent/db.py` is not covered by the
  example above and is an `undeclared-edge`; its message lists the narrowed
  declarations of the edge.
- **Cycles.** The local graph is still the graph between direct children: every
  endpoint is cut to its first component (`services -> agent`), and that graph
  must be acyclic. Narrowing cannot hide a cycle, and a pair whose two ends lie
  under the same child is a `manifest-self-edge`.
- **Stale.** Each declared pair needs a fact it covers. A narrowed pair that no
  fact matches is `manifest-stale-edge`, and `sync --prune` removes that value.
- **Existence.** Every path must be a tracked file or directory
  (`manifest-unknown-child` otherwise). Exports along the path are unchanged.

## sync --declare

`sync --declare` declares at child granularity and never widens a narrowing. When
the edge exists only as narrowed pairs, a fact from a source those pairs already
authorise gets its target added to that source (cut to the depth of the targets
already listed). A fact from any other source stays an error: whether another
part of the child may use the target is a decision, not a declaration.

Parent: [[meizel/docs/checker/checker.ava.okf.md|the checker]].
