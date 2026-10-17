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

Router mount: [[gateway/routers/docs/routers.meiva.okf.md]].
