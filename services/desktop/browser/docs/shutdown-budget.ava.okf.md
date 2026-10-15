---
type: doc
title: Browser shutdown budget
description: The roster stop window includes every sequential bounded cleanup step.
tags: [browser, lifecycle]
---

# Browser shutdown budget

`shutdown_budget.py` stays importable without the MCP SDK. In `mcp_daemon.run`,
upstream-stack cleanup and listener cleanup run sequentially, each bounded by
`SHUTDOWN_STEP_TIMEOUT_S`. Adding another sequential step requires updating
`SHUTDOWN_STEPS`; `SHUTDOWN_CEILING_S` is their combined budget. The roster
supplies this ceiling and the root manifest adds its stop margin.

`tests/components/services/test_ava_root_glue_manifests.py::test_browser_mcp_window_covers_every_bounded_shutdown_step`
checks the declared ceiling-to-root-window relationship. It does not count the
cleanup calls itself; changes to that sequence still need semantic review.
