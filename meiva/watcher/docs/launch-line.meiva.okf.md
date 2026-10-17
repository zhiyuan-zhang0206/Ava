---
type: doc
title: Watcher launch line
description: "How a watcher's generated launch line is sourced into its session shell."
tags: []
---

# Watcher launch line

```meiva-contract
id = "watcher-launch-line-sourced-from-disk"
kind = "invariant"
statement = """
A watcher's generated launch line is sourced from a file in the same session
shell, so long paths never sit in a canonical-mode PTY input buffer. Sourcing
preserves the command status, session exit and the watcher child's
orphan-guard parent.
"""
code = ["meiva/watcher/spawn.py:run"]
tests = ["meiva/tests/namespaces/watcher/test_watcher.py::test_spawn_line_tees_and_notifies"]
```

Parent: [[meiva/watcher/docs/watcher.meiva.okf.md]].
