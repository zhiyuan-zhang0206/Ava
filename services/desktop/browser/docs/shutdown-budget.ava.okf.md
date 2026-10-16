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

## Daemon test premises

`tests/test_browser_mcp_daemon_stop.py` runs the real daemon with a fake `npx`
upstream and a listening socket standing in for independently retained Chrome.
A successful `list_tools` round trip establishes the upstream connection before
normal-stop assertions. It does not exercise a real Chrome session.

The private home is under `/tmp` to keep the Unix socket within macOS's 104-byte
path limit. The fake upstream mode is baked into its script because the MCP
stdio environment does not pass arbitrary variables through. A private `.env`
declares the cluster-pinned CDP port, machine identity and a 120-second connect
timeout; remote gateway configuration variables are removed. The hung-connect
test requires SIGTERM to finish within 15 seconds, before that connect timeout.

Separate in-process cases inject cancelled sessions and wedged cleanup. They
check remaining cleanup and bounded completion under those injected conditions;
they do not establish that every possible shutdown fault completes successfully.
`CancelledError` is outside `Exception`, so suppressing only `Exception` while
awaiting a cancelled session can skip the remaining cleanup.
