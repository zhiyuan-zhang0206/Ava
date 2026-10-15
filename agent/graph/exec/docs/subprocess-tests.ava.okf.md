---
type: doc
title: Subprocess test assumptions
description: Timing and process-state premises of real execution-child tests.
---

# Subprocess test assumptions

`agent/tests/test_exec_subprocess.py` measures timeout from spawn. Cases that
assert interruption of user code or a spawned descendant must leave enough
budget for child imports and descendant creation. After SIGKILL, a descendant
can remain a zombie until its new parent reaps it; `pid_exists()` alone cannot
distinguish that state from a live process. Cleanup assertions check live members.
