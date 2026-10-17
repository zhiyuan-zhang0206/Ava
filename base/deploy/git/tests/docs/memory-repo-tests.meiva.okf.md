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

That recorder remains for initialization, which this refresh change does not
redesign. Removing its annotation introduces reportUnknownArgumentType at the
list append; it is not unused explanatory prose. Other initialization tests
retain their original role, no-remote, wrong-branch and bounded-command assertions.
