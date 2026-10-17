---
type: doc
title: Memory-graph fixture repo
description: The memory-graph router tests' fixture repo keeps git auto-maintenance disabled so a fresh checkout can be copied while no maintenance process runs.
tags:
- testing
- gateway
---

# Memory-graph fixture repo

`test_memory_graph.py` builds its memory-root repos with `_init_repo` - index
plus one note, one commit - and `test_two_apps_keep_memory_notes_and_graphs_separate`
copies the fresh checkout (`.git` included) to prove two apps keep their own
root. Automatic maintenance triggered by `git commit` detaches by default and
its background process can hold `.git/objects/maintenance.lock` past the
command's return; the copy then lists the lock and fails to read it
(`shutil.Error: [Errno 2] ... maintenance.lock`). `_init_repo` therefore
disables auto-maintenance on the repo it creates (`maintenance.auto=false`,
`gc.auto=0`), so `git commit` spawns no maintenance process at all.

```meiva-contract
id = "memory-graph-repo-disables-auto-maintenance"
kind = "external"
statement = "Automatic maintenance triggered by `git commit` detaches by default (maintenance.autoDetach, falling back to gc.autoDetach) and its background process keeps the repository's object-database lock (.git/objects/maintenance.lock) after the commit command returns; a fixture whose checkout is copied right after the commit disables auto-maintenance (maintenance.auto=false, gc.auto=0) so no maintenance process exists to race the copy."
code = ["gateway/tests/routers/test_memory_graph.py:_init_repo"]
tests = ["gateway/tests/routers/test_memory_graph.py::test_two_apps_keep_memory_notes_and_graphs_separate", "gateway/tests/routers/test_memory_graph.py::test_graph_builds_once_per_pool_revision"]
source = "https://git-scm.com/docs/git-maintenance"
applies_to = "git auto-maintenance detach and object-database lock (maintenance.autoDetach / gc.autoDetach defaults)"
```

Router mount: [[gateway/routers/docs/routers.meiva.okf.md]].
