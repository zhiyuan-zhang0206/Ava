---
type: doc
title: Memory repository tests
description: Real checkout refresh coverage and existing initialization command recorder.
tags: [tests, memory]
---

# Memory repository tests

Refresh cases use real temporary checkouts and a local bare remote. They
verify selected-branch contents, retained unpublished commits on divergence,
and keep-local operation with no remote. The gateway's Git hook test waits for
an unrelated request to complete, proving event-loop progress during Git work
without replacing `pull_main` or `asyncio.to_thread`.

```meiva-contract
id = "initialization-command-recorder"
kind = "invariant"
statement = "The existing initialization test records list-shaped subprocess argv and verifies that a fresh keep-local checkout initializes locally without clone or remote access. Its narrow Pyright annotation covers the dynamically typed argv item obtained through the existing subprocess replacement."
code = ["base/deploy/git/tests/test_memory_repo.py:test_init_keep_local_fresh_repo_inits_without_remote"]
tests = ["base/deploy/git/tests/test_memory_repo.py::test_init_keep_local_fresh_repo_inits_without_remote"]
```

That recorder remains for initialization, which this refresh change does not
redesign. Removing its annotation introduces reportUnknownArgumentType at the
list append; it is not unused explanatory prose. Other initialization tests
retain their original role, no-remote, wrong-branch and bounded-command assertions.
